import neuralxc
from abc import ABC, abstractmethod
import pyscf
from pyscf import gto, dft
from pyscf.dft import RKS
from pyscf.scf import hf, RHF, RKS
from pyscf.scf.chkfile import load_scf
from pyscf.lib.numpy_helper import NPArrayWithTag
import numpy as np
from scipy.special import sph_harm
import scipy.linalg
from sympy import N
from functools import reduce
import time
import math
from ..doc_inherit import doc_inherit
from spher_grad import grlylm
from ..base import ABCRegistry
from numba import jit
from ..timer import timer
from ..projector import DefaultProjector, BaseProjector
import neuralxc
import os

LAMBDA = 0.1

l_dict = {'s': 0, 'p': 1, 'd': 2, 'f': 3, 'g': 4, 'h': 5, 'i': 6, 'j': 7}
l_dict_inv = {l_dict[key]: key for key in l_dict}


def RKS(mol, nxc='', nxc_type='pyscf', **kwargs):
    """ Wrapper for the pyscf RKS (restricted Kohn-Sham) class
    that uses a NeuralXC potential
    """
    mf = dft.RKS(mol, **kwargs)
    if not nxc is '':
        model = neuralxc.get_nxc_adapter(nxc_type, nxc)
        if nxc_type=='pyscf_rad':
            mf.get_veff = veff_mod_rad(mf, model)
        else:
            model.initialize(mol)
            mf.get_veff = veff_mod(mf, model)
    return mf


def compute_KS(atoms, path='pyscf.chkpt', basis='ccpvdz', xc='PBE', nxc='',
    nxc_type='pyscf', approx_val=False):
    """ Given an ase atoms object, run a pyscf RKS calculation on it and
    return the results
    """
    pos = atoms.positions
    spec = atoms.get_chemical_symbols()
    mol_input = [[s, p] for s, p in zip(spec, pos)]

    mol = gto.M(atom=mol_input, basis=basis)
    if '.jit' in nxc:
        nxc_type='pyscf_rad'
    if approx_val:
        mf = dft.RKS(mol)
        mf.xc = xc
        mf.kernel()
        mf.mo_occ[1:] = 0
        dm_core = mf.make_rdm1()

    mf = RKS(mol, nxc=nxc, nxc_type=nxc_type)

    if approx_val:
        mf.dm_core = dm_core
    mf.set(chkfile=path)
    mf.xc = xc
    mf.kernel()
    if os.path.isfile('dm.ref_eval.npy') or os.path.isfile('dm.ref.npy'):
        try:
            dm_ref = np.load('dm.ref_eval.npy')
        except FileNotFoundError:
            dm_ref = np.load('dm.ref.npy')

        rho_ref = dft.numint.get_rho(mf._numint, mol, dm_ref, mf.grids)
        rho = dft.numint.get_rho(mf._numint, mol, mf.make_rdm1(), mf.grids)
        with open('rho_error','a') as file:
            file.write('{}\n'.format(np.sum((rho_ref-rho)**2*mf.grids.weights)))

    return mf, mol


def veff_mod(mf, model):
    """ Wrapper to get the modified get_veff() that uses a NeuralXC
    potential
    """
    def get_veff(mol=None, dm=None, dm_last=0, vhf_last=0, hermi=1):
        veff = pyscf.dft.rks.get_veff(mf, mol, dm, dm_last, vhf_last, hermi)
        vnxc = NPArrayWithTag(veff.shape)
        nxc = model.get_V(dm)
        vnxc[:, :] = nxc[1][:, :]
        vnxc.exc = nxc[0]
        vnxc.ecoul = 0
        veff[:, :] += vnxc[:, :]
        veff.exc += vnxc.exc
        return veff

    return get_veff

def veff_mod_rad(mf, model) :
    """ Wrapper to get the modified get_veff() that uses a NeuralXC
    potential
    """
    def eval_xc(xc_code, rho, spin=0, relativity=0, deriv=1, verbose=None):
        rho0 = rho[:1]
        gamma = None


        exc, V_nxc = model.get_V(rho0.flatten())

        if hasattr(model, 'rho_ref'):
            print('Using reference density')
            V_nxc += LAMBDA*(rho0.flatten()-model.rho_ref)
        exc = exc/rho0.flatten()
        exc = exc/model.grid_weights
        exc /= len(model.grid_weights)

        vrho = V_nxc

        vgamma = np.zeros_like(V_nxc)
        vlapl = None
        vtau = None
        vxc = (vrho , vgamma, vlapl, vtau)
        fxc = None  # 2nd order functional derivative
        kxc = None  # 3rd order functional derivative

        return exc, vxc, fxc, kxc

    def get_veff(mol=None, dm=None, dm_last=0, vhf_last=0, hermi=1):
        mf.define_xc_(mf.xc,'GGA')
        veff = pyscf.dft.rks.get_veff(mf, mol, dm, dm_last, vhf_last, hermi)
        mf.define_xc_(eval_xc,'GGA')
        if hasattr(mf,'dm_core'):
            dm_val = dm - mf.dm_core
        else:
            dm_val = dm

        model.initialize(mf.grids.coords,mf.grids.weights, mol)

        if os.path.isfile('dm.ref.npy'):
            dm_ref = np.load('dm.ref.npy')
            rho_ref = dft.numint.get_rho(mf._numint, mol, dm_ref, mf.grids)
            model.rho_ref = rho_ref

        vnxc = pyscf.dft.rks.get_veff(mf, mol, dm_val, dm_last,
            vhf_last, hermi)
        veff[:, :] += (vnxc[:, :] - vnxc.vj[:,:])
        veff.exc += vnxc.exc

        return veff
    return get_veff

def get_eri3c(mol, auxmol, op):
    """ Returns three center-one electron intergrals need for basis
    set projection
    """
    pmol = mol + auxmol
    nao = mol.nao_nr()
    naux = auxmol.nao_nr()
    if op == 'rij':
        eri3c = pmol.intor('int3c2e_sph', shls_slice=(0, mol.nbas, 0, mol.nbas, mol.nbas, mol.nbas + auxmol.nbas))
    elif op == 'delta':
        eri3c = pmol.intor('int3c1e_sph', shls_slice=(0, mol.nbas, 0, mol.nbas, mol.nbas, mol.nbas + auxmol.nbas))
    else:
        raise ValueError('Operator {} not implemented'.format(op))

    return eri3c.reshape(mol.nao_nr(), mol.nao_nr(), -1)


def get_dm(mo_coeff, mo_occ):
    """ Get density matrix"""
    return np.einsum('ij,j,jk -> ik', mo_coeff, mo_occ, mo_coeff.T)


def get_coeff(dm, eri3c):
    """ Given a density matrix, return coefficients from basis set projection
    """
    return np.einsum('ijk, ij -> k', eri3c, dm)


class PySCFProjector(BaseProjector):

    _registry_name = 'pyscf'

    def __init__(self, mol, basis_instructions, **kwargs):
        self.basis = basis_instructions
        self.initialize(mol)

    def initialize(self, mol, **kwargs):
        self.spec_agnostic = self.basis.get('spec_agnostic', False)
        self.op = self.basis.get('operator', 'delta').lower()
        self.delta = self.basis.get('delta', False)


        if self.delta:
            mf = RHF(mol)
            self.dm_init = mf.init_guess_by_atom()

        if self.spec_agnostic:
            basis = {}
            for atom_idx, _ in enumerate(mol.atom_charges()):
                sym = mol.atom_pure_symbol(atom_idx)
                if os.path.isfile(self.basis['basis']):
                    basis[sym] = gto.basis.parse(open(self.basis['basis'],'r').read())
                else:
                    basis[sym] = gto.basis.load(self.basis['basis'], 'O')
        else:
            basis = self.basis['basis']

        auxmol = gto.M(atom=mol.atom, basis=basis)
        self.bp = BasisPadder(auxmol)
        self.eri3c = get_eri3c(mol, auxmol, self.op)
        self.mol = mol
        self.auxmol = auxmol

    def get_basis_rep(self, dm, **kwargs):
        # if not mol is None and mol.atom != self.mol.atom:
        #     self.initialize(mol)
        if self.delta:
            dm = dm - self.dm_init
        coeff = get_coeff(dm, self.eri3c)
        coeff = self.bp.pad_basis(coeff)

        if self.spec_agnostic:
            self.spec_partition = {sym: len(coeff[sym]) for sym in coeff}
            coeff_agn = np.concatenate([coeff[sym] for sym in coeff], axis=0)
            coeff = {'X': coeff_agn}

        return coeff

    def get_V(self, dEdC, **kwargs):
        if self.spec_agnostic:
            running_idx = 0
            for sym in self.spec_partition:
                dEdC[sym] = dEdC['X'][:, running_idx:running_idx + self.spec_partition[sym]]
                running_idx += self.spec_partition[sym]

            dEdC.pop('X')
        dEdC = self.bp.unpad_basis(dEdC)
        V = np.einsum('ijk, k', self.eri3c, dEdC)
        return V


class BasisPadder():
    def __init__(self, mol):

        self.mol = mol

        max_l = {}
        max_n = {}
        sym_cnt = {}
        sym_idx = {}
        # Find maximum angular momentum and n for each species
        for atom_idx, _ in enumerate(mol.atom_charges()):
            sym = mol.atom_pure_symbol(atom_idx)
            if not sym in sym_cnt:
                sym_cnt[sym] = 0
                sym_idx[sym] = []
            sym_idx[sym].append(atom_idx)
            sym_cnt[sym] += 1

        for ao_idx, label in enumerate(mol.ao_labels(fmt=False)):
            sym = label[1]
            if not sym in max_l:
                max_l[sym] = 0
                max_n[sym] = 0

            n = int(label[2][:-1])
            max_n[sym] = max(n, max_n[sym])

            l = l_dict[label[2][-1]]
            max_l[sym] = max(l, max_l[sym])

        indexing_left = {sym: [] for sym in max_n}
        indexing_right = {sym: [] for sym in max_n}
        labels = mol.ao_labels()
        for sym in max_n:
            for idx in sym_idx[sym]:
                indexing_left[sym].append([])
                indexing_right[sym].append([])
                for n in range(1, max_n[sym] + 1):
                    for l in range(max_l[sym] + 1):
                        if any(['{} {} {}{}'.format(idx, sym, n, l_dict_inv[l]) in lab for lab in labels]):
                            indexing_left[sym][-1] += [True] * (2 * l + 1)
                            sidx = np.where(['{} {} {}{}'.format(idx, sym, n, l_dict_inv[l]) in lab
                                             for lab in labels])[0][0]
                            indexing_right[sym][-1] += np.arange(sidx, sidx + (2 * l + 1)).astype(int).tolist()
                        else:
                            indexing_left[sym][-1] += [False] * (2 * l + 1)

        self.sym_cnt = sym_cnt
        self.max_l = max_l
        self.max_n = max_n
        self.indexing_l = indexing_left
        self.indexing_r = indexing_right

    def get_basis_json(self):

        basis = {}

        for sym in self.sym_cnt:
            basis[sym] = {'n': self.max_n[sym], 'l': self.max_l[sym] + 1}

        if 'O' in basis:
            basis['X'] = {'n': self.max_n['O'], 'l': self.max_l['O'] + 1}

        return basis

    def pad_basis(self, coeff):
        # Mimu = None
        coeff_out = {
            sym: np.zeros([self.sym_cnt[sym], self.max_n[sym] * (self.max_l[sym] + 1)**2])
            for sym in self.indexing_l
        }

        cnt = {sym: 0 for sym in self.indexing_l}

        for aidx, slice in enumerate(self.mol.aoslice_by_atom()):
            sym = self.mol.atom_pure_symbol(aidx)
            coeff_out[sym][cnt[sym], self.indexing_l[sym][cnt[sym]]] = coeff[slice[-2]:slice[-1]][
                np.array(self.indexing_r[sym][cnt[sym]]) - slice[-2]]
            cnt[sym] += 1

        return coeff_out

    def unpad_basis(self, coeff):

        cnt = {sym: 0 for sym in self.indexing_l}
        coeff_out = np.zeros(len(self.mol.ao_labels()))
        for aidx, slice in enumerate(self.mol.aoslice_by_atom()):
            sym = self.mol.atom_pure_symbol(aidx)
            coeff_in = coeff[sym]
            if coeff_in.ndim == 3: coeff_in = coeff_in[0]
            coeff_out[slice[-2]:slice[-1]][np.array(self.indexing_r[sym][cnt[sym]]) -
                                           slice[-2]] = coeff_in[cnt[sym], self.indexing_l[sym][cnt[sym]]]
            cnt[sym] += 1

        return coeff_out
