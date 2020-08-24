from abc import ABC, abstractmethod
import numpy as np
from scipy.special import sph_harm
import scipy.linalg
from sympy import N
from functools import reduce
import time
import math
from ..doc_inherit import doc_inherit
from spher_grad import grlylm
from ..timer import timer
import neuralxc.config as config
from ..utils import geom
import pyscf.gto.basis as gtobasis
import pyscf.gto as gto
from .projector import OrthoProjector, DefaultProjector,BaseProjector, RadialProjector
import neuralxc

GAMMA = np.array([1/2,3/4,15/8,105/16,945/32,10395/64,135135/128])*np.sqrt(np.pi)

class GaussianProjector(DefaultProjector):

    _registry_name = 'gaussian'
    _unit_test = False

    def __init__(self, unitcell, grid, basis_instructions, **kwargs):

        full_basis, basis_strings = parse_basis(basis_instructions)
        basis = {key:val for key,val in basis_instructions.items()}
        basis.update(full_basis)
        self.basis_strings = basis_strings
        DefaultProjector.__init__(self, unitcell, grid, basis, **kwargs)

    def get_basis_rep(self, rho, positions, species, **kwargs):
        """Calculates the basis representation for a given real space density

        Parameters
        ------------------
        rho, array, float
        	Electron density in real space
        positions, array float
        	atomic positions
        species, list string
        	atomic species (chem. symbols)

        Returns
        ------------
        c, dict of np.ndarrays
        	Basis representation, dict keys correspond to atomic species.
        """
        basis_rep = {}
        for pos, spec in zip(positions, species):
            if not spec in basis_rep:
                basis_rep[spec] = []

            idx = '{}{}{}{}'.format(spec, pos[0], pos[1], pos[2])
            basis = self.basis[spec]
            # box = self.box_around(pos, basis['r_o'])
            projection, angs = self.project(rho,pos, basis,self.basis_strings[spec], angs=self.all_angs.get(idx, None))
            basis_rep[spec].append(projection)
            if config.UseMemory:
                self.all_angs[idx] = angs

        for spec in basis_rep:
            basis_rep[spec] = np.concatenate(basis_rep[spec], axis=0)

        return basis_rep


    def project(self, rho, pos, basis_instructions, basis_string, angs=None):
        '''
            Project the real space density rho onto a set of basis functions

            Parameters
            ----------
                rho: np.ndarray
                    electron charge density on grid
                box: dict
                     contains the mesh in spherical and euclidean coordinates,
                     can be obtained with get_box_around()
                n_rad: int
                     number of radial functions
                n_l: int
                     number of spherical harmonics
                r_o: float
                     outer radial cutoff in Angstrom
                W: np.ndarray
                     matrix used to orthonormalize radial basis functions

            Returns
            --------
                dict
                    dictionary containing the coefficients
            '''

        for i in range(1):
            coeff = []
            r_o_max = np.max([np.max(b['r_o']) for b in basis_instructions])
            box = self.box_around(pos, r_o_max)
            rho_small = rho[[box['mesh'][i] for i in range(3)]]
            box['radial'] = np.stack(box['radial'])
            if isinstance(self.V_cell, np.ndarray) == 1:
                box['mesh'] = np.stack(box['mesh'][0:1])
            else:
                box['mesh'] = np.stack(box['mesh'])

            for ib, basis in enumerate(basis_instructions):
                l = basis['l']
                r_o_max = np.max(basis['r_o'])
                filt = (box['radial'][0] <= r_o_max)
                box_rad = box['radial'][:,filt]
                box_m = box['mesh'][:,filt]
                ang = self.angulars_real(l, *box_rad[1:]) # shape (m, x, y, z)
                rad = np.stack(self.radials(box_rad[0], [basis])[0]) # shape (n, x, y, z)
                if isinstance(self.V_cell,np.ndarray):
                    V_cell = self.V_cell[box_m[0]]
                else:
                    V_cell = self.V_cell
                rad *= V_cell
                if rho.ndim == 1:
                    c = np.einsum('i,mi,ni -> nm', rho[box_m[0]], ang, rad)
                else:
                    c = np.einsum('i,mi,ni -> nm', rho_small[filt], ang, rad)
                coeff += c.flatten().tolist()

        end = time.time()
        mol = gto.M(atom='O 0 0 0',
                    basis={'O': gtobasis.parse(basis_string)})
        bp = neuralxc.pyscf.BasisPadder(mol)
        coeff = bp.pad_basis(np.array(coeff))['O']
        return np.array(coeff).reshape(1, -1), None


    @classmethod
    def g(cls, r, r_o, alpha, l, coeff=[1]):
        fc = 1-(.5*(1-np.cos(np.pi*r/np.max(r_o))))**8
        f = np.zeros_like(r)
        N = 0
        for a, c in zip(alpha, coeff):
            N += (2*a)**(l/2+3/4)*np.sqrt(2)/np.sqrt(GAMMA[l])
            f += np.exp(-a*r**2)*c

        f *= (r**l*fc*N)
        f[r>np.max(r_o)] = 0
        return f

    @classmethod
    def get_W(cls, basis):
        return np.eye(3)

    @classmethod
    def radials(cls, r, basis, W = None):
        result = []
        if isinstance(basis, list):
            for b in basis:
                res = []
                for ib, alpha in enumerate(b['alpha']):
                    res.append(cls.g(r, b['r_o'][ib], b['alpha'][ib], b['l'],b['coeff'][ib]))
                result.append(res)
        elif isinstance(basis, dict):
                result.append([cls.g(r, basis['r_o'], basis['alpha'], basis['l'], b['coeff'])])
        return result

class RadialGaussianProjector(GaussianProjector):

    _registry_name = 'gaussian_radial'
    _unit_test = False

    def __init__(self, basis_instructions, grid_coords, grid_weights, **kwargs):
        self.grid_coords = grid_coords
        self.grid_weights = grid_weights
        self.V_cell = self.grid_weights
        full_basis, basis_strings = parse_basis(basis_instructions)
        basis = {key:val for key,val in basis_instructions.items()}
        basis.update(full_basis)
        self.basis_strings = basis_strings
        self.basis = basis
        self.all_angs = {}

    box_around = RadialProjector.box_around

def parse_basis(basis_instructions):
    full_basis = {}
    basis_strings = {}
    for species in basis_instructions:
        if len(species) < 3:
            basis_strings[species] = open(basis_instructions[species]['basis'],'r').read()
            bas = gtobasis.parse(basis_strings[species])
            mol = gto.M(atom='O 0 0 0', basis = {'O':bas})
            sigma = basis_instructions[species].get('sigma',2.0)
            basis = {}
            for bi in range(mol.atom_nshells(0)):
                l = mol.bas_angular(bi)
                if l not in basis:
                    basis[l] = {'alpha':[],'r_o':[],'coeff':[]}
                # alpha = np.array(b[1:])[:,0]
                alpha = mol.bas_exp(bi)
                coeff = mol.bas_ctr_coeff(bi)
                r_o = alpha**(-1/2)*sigma*(1+l/5)
                basis[l]['alpha'].append(alpha)
                basis[l]['r_o'].append(r_o)
                basis[l]['coeff'].append(coeff)
            basis = [{'l': l,'alpha': basis[l]['alpha'],'r_o': basis[l]['r_o'],'coeff':basis[l]['coeff']} for l in basis]
            full_basis[species] = basis
    return full_basis, basis_strings