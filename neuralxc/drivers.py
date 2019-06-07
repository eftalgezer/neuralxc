import json
import glob
import h5py
from ase.io import read
from neuralxc.symmetrizer import symmetrizer_factory
from neuralxc.formatter import atomic_shape, system_shape, SpeciesGrouper
from neuralxc.ml.transformer import GroupedPCA, GroupedVarianceThreshold
from neuralxc.ml.transformer import GroupedStandardScaler
from neuralxc.ml import NetworkEstimator as NetworkWrapper
from neuralxc.ml import NXCPipeline
from neuralxc.ml.ensemble import StackedEstimator,ChainedEstimator
from neuralxc.ml.network import load_pipeline, NumpyNetworkEstimator
from neuralxc.preprocessor import Preprocessor
from neuralxc.datastructures.hdf5 import *
from neuralxc.ml.utils import *
from sklearn.model_selection import GridSearchCV
from sklearn.pipeline import Pipeline
from sklearn.base import clone
import pandas as pd
from pprint import pprint
from dask.distributed import Client, LocalCluster
from sklearn.externals.joblib import parallel_backend
import time
import os
import shutil
from collections import namedtuple
import hashlib
import subprocess
import matplotlib.pyplot as plt
import numpy as np
import neuralxc as xc
import sys
import copy
import pickle
from types import SimpleNamespace as SN
os.environ['KMP_AFFINITY'] = 'none'
os.environ['PYTHONWARNINGS'] = 'ignore::DeprecationWarning'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '10'
def plot_basis(args):
    """ Plots a set of basis functions specified in .json file"""

    basis_instructions = json.loads(open(args.basis,'r').read())
    projector = xc.projector.DensityProjector(np.eye(3),np.ones(3),
        basis_instructions['basis'])

    for spec in basis_instructions['basis']:
        if not len(spec) == 1: continue
        basis = basis_instructions['basis'][spec]
        n = basis_instructions['basis'][spec]['n']
        W = projector.get_W(basis)
        r = np.linspace(0,basis['r_o'],500)
        radials = projector.radials(r,basis,W)
        for rad in radials:
            plt.plot(r, rad)
        plt.show()

def convert_tf(args):
    """ Converts the tensorflow estimator inside a NXCPipeline to a simple
    numpy base estimator"""

    nxc_tf = xc.NeuralXC(args.tf)
    pipeline = nxc_tf._pipeline

    #Needs to do a fake run to build the tensorflow graph
    unitcell = np.eye(3)*20
    grid = [40]*3
    rho = np.zeros(grid)

    species = [key for key in pipeline.get_basis_instructions() if len(key) == 1]
    positions = np.zeros([len(species),3])

    nxc_tf.initialize(unitcell, grid, positions, species)
    # nxc_tf.get_V(rho)
    C = nxc_tf.projector.get_basis_rep(rho, positions, species)
    D = nxc_tf.symmetrizer.get_symmetrized(C)
    nxc_tf._pipeline.predict(D)
    nxc_tf._pipeline.save(args.np, True, True)

def merge_driver(args):
    """ Converts the tensorflow estimator inside a NXCPipeline to a simple
    numpy base estimator"""

    nxc_tf = xc.NeuralXC(args.chained)
    pipeline = nxc_tf._pipeline

    label, estimator = pipeline.steps[-1]
    _ , npestimator = pipeline.steps[-2]

    if not isinstance(npestimator, NumpyNetworkEstimator):
        raise Exception('Something went wrong. Second to last pipeline element'\
        +' must be NumpyNetworkEstimator')

    if not isinstance(estimator, NumpyNetworkEstimator):
        if not isinstance(estimator, NetworkWrapper):
            raise Exception('Something went wrong. Last pipeline element'\
            +' must be an estimator')
        else:
            convert_tf(namedtuple('tuplename',('tf','np'))(args.chained, args.merged))
            args.chained = args.merged

            nxc_tf = xc.NeuralXC(args.chained)
            pipeline = nxc_tf._pipeline

            label, estimator = pipeline.steps[-1]
            _ , npestimator = pipeline.steps[-2]

    if not npestimator.trunc:
            npestimator = npestimator.trunc_after(-1)

    pipeline.steps[-2] = (label, ChainedEstimator([npestimator, estimator]).merge())
    pipeline.steps = pipeline.steps[:-1]
    nxc_tf._pipeline.save(args.merged, True, True)

def add_data_driver(args):
    """ Adds data to hdf5 file"""
    try:
        file = h5py.File(args.hdf5 ,'r+')
    except OSError:
        file = h5py.File(args.hdf5 ,'w')

    i,j,k = [(None if a == '' else int(a)) for a in args.slice.split(':')] +\
        [None]*(3-len(args.slice.split(':')))

    ijk = slice(i,j,k)

    def obs(which):
        if which == 'energy':
            if args.traj:
                add_species(file, args.system, args.traj)
                energies = np.array([a.get_potential_energy()\
                 for a in read(args.traj,':')])[ijk]
                add_energy(file, energies, args.system, args.method, args.override)
            else:
                raise Exception('Must provide a trajectory file')
                file.close()
        elif which == 'forces':
            if args.traj:
                add_species(file, args.system, args.traj)
                forces = np.array([a.get_forces()\
                 for a in read(args.traj,':')])[ijk]
                add_forces(file, forces, args.system, args.method, args.override)
            else:
                raise Exception('Must provide a trajectory file')
                file.close()
        elif which == 'density':
            add_species(file, args.system, args.traj)
            species = file[args.system].attrs['species']
            data = np.load(args.density)[ijk]
            add_density((args.density.split('/')[-1]).split('.')[0], file, data,
                args.system, args.method, args.override)
        else:
            raise Exception('Option {} not recognized'.format(which))

    if args.density and not 'density' in args.add:
        args.add.append('density')
    for observable in args.add:
        obs(observable)

    file.close()


def split_data_driver(args):
    """ Split dataset (or all data inside a group) by providing slices"""
    file = h5py.File(args.hdf5 ,'r+')

    i,j,k = [(None if a == '' else int(a)) for a in args.slice.split(':')] +\
        [None]*(3-len(args.slice.split(':')))

    ijk = slice(i,j,k)


    root = args.group
    if not root[0] == '/': root = '/' + root

    def collect_all_sets(file, path):
        sets = {}
        if isinstance(file[path],h5py._hl.dataset.Dataset):
            return {path: file[path]}
        else:
            for key in file[path]:
                sets.update(collect_all_sets(file, path + '/' + key))
        return sets

    all_sets = collect_all_sets(file, root)
    split_sets = {}
    comp_sets = {}
    length = -1
    for path in all_sets:
        new_len = len(all_sets[path][:])
        if length == -1:
            length = new_len
        elif new_len != length:
            raise Exception('Datasets contained in group dont have consistent lengths')
        idx = path.find(args.group) + len(args.group)
        new_path = path[:idx] +'/' + args.label + path[idx:]
        if args.comp != '':
            idx = path.find(args.group) + len(args.group)
            comp_path = path[:idx] +'/' + args.comp + path[idx:]
            comp_sets[comp_path] = all_sets[path][:].tolist()
            del comp_sets[comp_path][ijk]
        split_sets[new_path] = all_sets[path][ijk]

    for new_path in split_sets:
        file.create_dataset(new_path, data = split_sets[new_path])

    for new_path, path in zip(split_sets, all_sets):
        file['/'.join(new_path.split('/')[:-1])].attrs.update(file['/'.join(path.split('/')[:-1])].attrs)
    if comp_sets:
        for comp_path in comp_sets:
            file.create_dataset(comp_path, data = comp_sets[comp_path])
        for new_path, path in zip(comp_sets, all_sets):
                file['/'.join(new_path.split('/')[:-1])].attrs.update(file['/'.join(path.split('/')[:-1])].attrs)
    # print(split_sets)

def delete_data_driver(args):
    """ Deletes data in hdf5 file"""
    file = h5py.File(args.hdf5 ,'r+')
    root = args.group
    if not root[0] == '/': root = '/' + root
    del file[root]

def parse_sets_input(path):
    """ Reads a file containing the sets used for fitting

    Parameters
    ----------
    path: str
        Path to file containing dataset names

    Returns
    --------
    hdf5, list
        hdf5[0] : datafile location
        hdf5[1],hdf5[2]: lists of baseline(,target) datasets
    """
    hdf5=['',[],[]]
    with open(path, 'r') as setsfile:
        line = setsfile.readline().rstrip()
        hdf5[0] = line #datafile location
        line = setsfile.readline().rstrip()
        while(line):
            split = line.split()
            hdf5[1].append(split[0])
            hdf5[2].append(split[1])
            line = setsfile.readline().rstrip()
    return hdf5


def model_driver(args):
    pass


def mkdir(dirname):
    try:
        os.mkdir(dirname)
    except FileExistsError:
        pass


def workflow_driver(args):

    if args.data:
        mkdir('it0')
        shutil.copy(args.data, 'it0/data.hdf5')
        shutil.copy(args.preprocessor, 'it0/pre.json')
        shutil.copy(args.config, 'it0/hyper.json')
        os.chdir('it0')
        open('sets.inp','w').write('data.hdf5 \n system/base \t system/ref')

        statistics_sc = \
        eval_driver(SN(model = '',hdf5=['data.hdf5','system/base',
                'system/ref'],plot=False,savefig=False,cutoff=0.0,predict=False))

        open('statistics_sc','w').write(json.dumps(statistics_sc))
        statistics_fit = fit_driver(SN(preprocessor='pre.json',config='hyper.json',mask=False, sample='',
                    cutoff=0.0, model = '',ensemble=False,
                    sets='sets.inp', hyperopt=True))
        open('statistics_fit','w').write(json.dumps(statistics_fit))
        convert_tf(SN(tf='best_model',np='merged_new'))
        os.chdir('../')
    else:
        iteration = 0
        print('====== Iteration {} ======'.format(iteration))
        mkdir('it{}'.format(iteration))
        shutil.copy(args.preprocessor, 'it{}/pre.json'.format(iteration))
        shutil.copy(args.config, 'it{}/hyper.json'.format(iteration))
        os.chdir('it{}'.format(iteration))
        open('sets.inp','w').write('data.hdf5 \n system/it{} \t system/ref'.format(iteration))
        mkdir('workdir')
        subprocess.Popen(open('../' + args.engine,'r').read(), shell=True).wait()
        pre_driver(SN(preprocessor='pre.json',dest='data.hdf5/system/it{}'.format(iteration),
                        mask = False, xyz=False))
        add_data_driver(SN(hdf5='data.hdf5',system='system',method='ref',add=['energy','forces'],
                        traj ='../sampled.traj', density='',override=True, slice=':'))
        statistics_sc = \
        eval_driver(SN(model = '',hdf5=['data.hdf5','system/it{}'.format(iteration),
                'system/ref'],plot=False,savefig=False,cutoff=0.0,predict=False))

        open('statistics_sc','w').write(json.dumps(statistics_sc))
        statistics_fit = fit_driver(SN(preprocessor='pre.json',config='hyper.json',mask=False, sample='',
                    cutoff=0.0, model = '',ensemble=False,
                    sets='sets.inp', hyperopt=True))

        open('statistics_fit','w').write(json.dumps(statistics_fit))
        convert_tf(SN(tf='best_model',np='merged_new'))

        os.chdir('../')
    open('siesta.fdf','a').write('\nNeuralXC ../../merged\n')
    for iteration in range(1, args.maxit +1):
        print('====== Iteration {} ======'.format(iteration))
        mkdir('it{}'.format(iteration))
        shutil.copy('it{}/data.hdf5'.format(iteration - 1),'it{}/data.hdf5'.format(iteration))
        shutil.copy(args.preprocessor, 'it{}/pre.json'.format(iteration))
        if args.config2:
            shutil.copy(args.config2, 'it{}/hyper.json'.format(iteration))
        else:
            shutil.copy(args.config, 'it{}/hyper.json'.format(iteration))
        shutil.copytree('it{}/merged_new'.format(iteration - 1),'it{}/merged'.format(iteration))
        os.chdir('it{}'.format(iteration))
        open('sets.inp','w').write('data.hdf5 \n *system/it{} \t system/ref'.format(iteration))
        mkdir('workdir')
        subprocess.Popen(open('../' + args.engine,'r').read(), shell=True).wait()
        pre_driver(SN(preprocessor='pre.json',dest='data.hdf5/system/it{}'.format(iteration),
                        mask = False, xyz=False))

        old_statistics = dict(statistics_sc)
        statistics_sc = \
        eval_driver(SN(model = '',hdf5=['data.hdf5','system/it{}'.format(iteration),
                'system/ref'],plot=False,savefig=False,cutoff=0.0,predict=False))

        open('statistics_sc','w').write(json.dumps(statistics_sc))

        if old_statistics['mae'] - statistics_sc['mae'] < args.tol:
            print('Iterative traning converged: dMAE = {} eV'.format(old_statistics['mae'] - statistics_sc['mae']))
            os.chdir('../')
            break
        chain_driver(SN(config='hyper.json', model ='merged',dest ='chained'))
        statistics_fit = fit_driver(SN(preprocessor='pre.json',config='hyper.json',mask=False, sample='',
                    cutoff=0.0, model = 'chained',ensemble=False,
                    sets='sets.inp', hyperopt=True))

        open('statistics_fit','w').write(json.dumps(statistics_fit))
        if statistics_fit['mae'] > statistics_sc['mae'] + args.tol:
            print('Stopping iterative training because fitting error is larger than self-consistent error')
            os.chdir('../')
            break
        merge_driver(SN(chained='best_model',merged='merged_new'))

        os.chdir('../')



def fit_driver(args):
    """ Fits a NXCPipeline to the provided data
    """
    inputfile = args.config
    preprocessor = args.preprocessor

    if args.sets != '':
        hdf5 = parse_sets_input(args.sets)
    else:
        hdf5 = args.hdf5

    mask = args.mask

    if not mask:
        inp = json.loads(open(inputfile,'r').read())
        pre = json.loads(open(preprocessor,'r').read())
    else:
        inp = {}
        pre = {}

    apply_to = []
    for pidx, path in enumerate(hdf5[1]):
        if path[0] == '*':
            apply_to.append(pidx)
            hdf5[1][pidx] = path[1:]

    grid_cv = get_grid_cv(hdf5, preprocessor, inputfile, mask)
    if mask: return 0

    new_model = grid_cv.estimator
    param_grid = grid_cv.param_grid
    param_grid = {key: param_grid[key][0] for key in param_grid}
    new_model.set_params(**param_grid)

    if args.model:
        if args.ensemble:
            new_model.steps[-1][1].steps[2:-1] =  xc.ml.network.load_pipeline(args.model).steps[:-1]
        else:
            new_model.steps[-1][1].steps[2:] =  xc.ml.network.load_pipeline(args.model).steps

    datafile = h5py.File(hdf5[0],'r')
    basis_key = basis_to_hash(pre['basis'])
    data = load_sets(datafile, hdf5[1], hdf5[2], basis_key, args.cutoff)

    if args.model:
        if new_model.steps[-1][1].steps[-1][1]._network == None:
            pipeline = Pipeline(new_model.steps[-1][1].steps[:-1])
        else:
            pipeline = new_model
        for set in apply_to:
            selection = (data[:,0] == set)
            prediction = pipeline.predict(data)[set]
            print('Dataset {} old STD: {}'.format(set, np.std(data[selection][:,-1])))
            data[selection,-1] += prediction
            print('Dataset {} new STD: {}'.format(set, np.std(data[selection][:,-1])))
    if args.sample != '':
        sample = np.load(args.sample)
        data = data[sample]
        print("Using sample of size {}".format(len(sample)))

    class FilePipeline():
        def __init__(self, path):
            self.path = path
            self.pipeline = pickle.load(open(self.path,'rb'))

        def predict(self, X, *args, **kwargs):
            return self.pipeline.predict(X, *args, **kwargs)

        def fit(self, X, y=None):
            return self

        def transform(self, X, y=None, **fit_params):
            return self.pipeline.transform(X, **fit_params)

        def fit_transform(self, X, y=None):
            return self.pipeline.transform(X)

    np.random.shuffle(data)
    if args.hyperopt:
        if args.model:
            if (new_model.steps[-1][1].steps[-2][1], NumpyNetworkEstimator):
                new_param_grid = {key[len('ml__'):]: value for key,value in  grid_cv.param_grid.items()\
                    if 'ml__estimator' in key}


                pickle.dump(Pipeline(new_model.steps[-1][1].steps[2:-1]),open('.tmp.pckl','wb'))
                estimator = GridSearchCV(Pipeline(new_model.steps[-1][1].steps[:2] + [('file_pipe', FilePipeline('.tmp.pckl')),
                                                    ('estimator', new_model.steps[-1][1].steps[-1][1])]),
                                                    new_param_grid, cv=inp.get('cv',2))


                new_model.steps[-1][1].steps = new_model.steps[-1][1].steps[:-1]
                do_concat = True
            else:
                raise Exception('Cannot optimize hyperparameters for fitted model')
        else:
            estimator = grid_cv
            do_concat = False
    else:
        estimator = new_model

    estimator.fit(data)

    dev = estimator.predict(data)[0].flatten() - data[:,-1].real.flatten()
    dev0 = np.abs(dev - np.mean(dev))
    results = {'mean deviation' : np.mean(dev).round(4), 'rmse': np.std(dev).round(4),
               'mae' : np.mean(dev0).round(4),'max': np.max(dev0).round(4)}

    if args.hyperopt:
        open('best_params.json','w').write(json.dumps(estimator.best_params_, indent=4))
        pd.DataFrame(estimator.cv_results_).to_csv('cv_results.csv')
        best_params_ = estimator.best_params_
        if do_concat:
            os.remove('.tmp.pckl')
            new_model.steps[-1][1].steps.append(estimator.best_estimator_.steps[-1])
            new_model.steps[-1][1].start_at(2).save('best_model',True)
        else:
            best_estimator = estimator.best_estimator_.steps[-1][1].start_at(2)
            best_estimator.save('best_model',True)
    else:
        estimator = estimator.steps[-1][1]
        estimator.start_at(2).save('best_model',True)
    return results

def chain_driver(args):

    inputfile = args.config

    inp = json.loads(open(inputfile,'r').read())

    param_grid = {key[len('estimator__'):]:value for key,value in inp['hyperparameters'].items() if 'estimator' in key}
    new_model = NetworkWrapper(**param_grid)

    old_model = xc.ml.network.load_pipeline(args.model)

    old_estimator = old_model.steps[-1][1]
    if not isinstance(old_estimator, xc.ml.network.NumpyNetworkEstimator):
        raise Exception('Currently only supported if saved model is NumpyNetworkEstimator,\
        but is ', type(old_estimator))

    old_model.steps[-1] = ('frozen_estimator', old_model.steps[-1][1])
    old_model.steps += [('estimator', new_model)]
    old_model.save(args.dest, True)

def sample_driver(args):
    """ Given a dataset, perform sampling in feature space"""

    preprocessor = args.preprocessor
    hdf5 = args.hdf5

    pre = json.loads(open(preprocessor,'r').read())

    datafile = h5py.File(hdf5[0],'r')
    basis_key = basis_to_hash(pre['basis'])
    data = load_sets(datafile, hdf5[1], hdf5[1], basis_key, args.cutoff)
    basis = pre['basis']
    symmetrizer_instructions = {'symmetrizer_type' :'casimir'}
    symmetrizer_instructions.update({'basis' : basis})
    species =  [''.join(find_attr_in_tree(datafile, hdf5[1], 'species'))]
    spec_group = SpeciesGrouper(basis, species)
    symmetrizer = symmetrizer_factory(symmetrizer_instructions)

    sampler_pipeline = get_default_pipeline(basis, species, pca_threshold = 1)
    sampler_pipeline = Pipeline(sampler_pipeline.steps)
    sampler_pipeline.steps[-1] = ('sampler', SampleSelector(args.size))
    sampler_pipeline.fit(data)
    sample = sampler_pipeline.predict(data)
    np.save(args.dest, np.array(sample).flatten())

def eval_driver(args):
    """ Evaluate fitted NXCPipeline on dataset and report statistics
    """
    hdf5 = args.hdf5

    if args.predict:
        hdf5.append(hdf5[1])
        cutoff = 0
    else:
        cutoff = args.cutoff
    datafile = h5py.File(hdf5[0],'r')

    if not args.model == '':
        model = xc.NeuralXC(args.model)._pipeline
        basis = model.get_basis_instructions()
        basis_key = basis_to_hash(basis)
    else:
        basis_key = ''

    data = load_sets(datafile, hdf5[1], hdf5[2], basis_key, cutoff)

    if not args.model == '':
        symmetrizer_instructions = model.get_symmetrize_instructions()
        symmetrizer_instructions.update({'basis' : basis})
        species =  [''.join(find_attr_in_tree(datafile, hdf5[1], 'species'))]
        spec_group = SpeciesGrouper(basis, species)
        symmetrizer = symmetrizer_factory(symmetrizer_instructions)
        print('Symmetrizer instructions', symmetrizer_instructions)
        pipeline = NXCPipeline([('spec_group', spec_group), ('symmetrizer', symmetrizer)
            ] + model.steps, basis_instructions=basis,
             symmetrize_instructions=symmetrizer_instructions)

        targets = data[:,-1].real
        predictions = pipeline.predict(data)[0]
        if args.predict:
            np.save(args.dest, predictions)
            return 0
        dev = (predictions.flatten() - targets.flatten())
    else:
        if args.predict:
            raise Exception('Must provide a model to make predictions')
        dev = data[:,-1].real
    dev0 = np.abs(dev - np.mean(dev))
    results = {'mean deviation' : np.mean(dev).round(4), 'rmse': np.std(dev).round(4),
               'mae' : np.mean(dev0).round(4),'max': np.max(dev0).round(4)}
    pprint(results)
    if args.plot:
        if args.model =='':
            plt.hist(dev.flatten())
            plt.xlabel('Target energies [eV]')
            plt.show()
        else:
            targets -= np.mean(targets)
            predictions -= np.mean(predictions)
            maxlim = np.max([np.max(targets),np.max(predictions)])
            minlim = np.min([np.max(targets),np.min(predictions)])
            plt.plot(targets.flatten(),predictions.flatten(),ls ='',marker='.')
            plt.plot([minlim,maxlim],[minlim,maxlim],ls ='-',marker='',color = 'grey')
            plt.xlabel('$E_{ref}[eV]$')
            plt.ylabel('$E_{pred}[eV]$')
            plt.show()
    return results

def ensemble_driver(args):

    all_pipelines = []
    for model_path in args.models:
        all_pipelines.append(xc.ml.network.load_pipeline(model_path))

    #Check for consistency
    # for pidx, pipeline in enumerate(all_pipelines):
        # for step0, step1 in zip(all_pipelines[0].steps[:-1], pipeline.steps[:-1]):
            # if not pickle.dumps(step0[1]) == pickle.dumps(step1[1]):
                # raise Exception('Parameters for {} in model {} inconsistent'.format(type(step0[1]), pidx))

    all_networks = [pipeline.steps[-1][1] for pipeline in all_pipelines]
    ensemble = StackedEstimator(all_networks, operation = args.operation)
    pipeline = all_pipelines[0]
    pipeline.steps[-1] = ('estimator', ensemble)

    pipeline.save(args.dest, override=True)



def pre_driver(args):
    """ Preprocess electron densities obtained from electronic structure
    calculations
    """
    preprocessor = args.preprocessor
    dest = args.dest
    xyz = args.xyz
    mask = args.mask

    if not mask:
        pre = json.loads(open(preprocessor,'r').read())
    else:
        pre = {}

    if 'traj_path' in pre and pre['traj_path'] != '':
        atoms = read(pre['traj_path'], ':')
        trajectory_path = pre['traj_path']
    elif xyz != '':
        atoms = read(xyz, ':')
        trajectory_path = xyz
    else:
        raise ValueError('Must specify path to to xyz file')

    preprocessor = get_preprocessor(preprocessor, mask, xyz)
    if not mask:
        start = time.time()

        if 'hdf5' in dest:
            dest_split = dest.split('/')
            file, system, method = dest_split + ['']*(3-len(dest_split))
            workdir = '.tmp'
            delete_workdir = True
        else:
            workdir = dest
            delete_workdir = False

        try:
            os.mkdir(workdir)
        except FileExistsError:
            delete_workdir = False
            pass
        print('======Projecting onto basis sets======')
        basis_grid = get_basis_grid(pre)['preprocessor__basis_instructions']

        for basis_instr in basis_grid:
            preprocessor.basis_instructions = basis_instr
            filename = os.path.join(workdir,basis_to_hash(basis_instr) + '.npy')
            data = preprocessor.fit_transform(None)
            np.save(filename, data)
            if 'hdf5' in dest:
                data_args = namedtuple(\
                'data_ns','hdf5 system method density slice add traj override')(\
                file,system,method,filename, ':',[],trajectory_path, True)
                add_data_driver(data_args)

                data_args = namedtuple(\
                'data_ns','hdf5 system method density slice add traj override')(\
                file,system,method,'', ':',['energy','forces'],pre['src_path'] + '/results.traj', True)
                add_data_driver(data_args)

        if delete_workdir:
            shutil.rmtree(workdir)
