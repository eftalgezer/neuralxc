from sklearn.base import TransformerMixin
from sklearn.base import BaseEstimator
from sklearn.feature_selection import VarianceThreshold
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from ..formatter import atomic_shape, system_shape
from abc import ABC, abstractmethod
import numpy as np

class GroupedTransformer(ABC):

    @abstractmethod
    def get_gradient(self):
        pass

    def transform(self, X,y=None, **fit_params):
        was_tuple = False
        if isinstance(X,tuple):
            y = X[1]
            X = X[0]
            was_tuple = True

        made_list = False
        if not isinstance(X, list):
            X = [X]
            made_list = True

        results = []
        for x in X:
            if isinstance(x, dict):
                results_dict = {}
                for spec in x:
                    results_dict[spec] = self._spec_dict[spec].transform(x[spec])
                results.append(results_dict)
            else:
                results.append(system_shape(super().transform(atomic_shape(x)),
                    x.shape[-2]))

        if made_list:
            results = results[0]
        if was_tuple:
            return results, y
        else:
            return results

    def fit(self,X, y=None):

        if isinstance(X, tuple):
            X = X[0]

        if isinstance(X, list):
            X = X[0]

        if isinstance(X, dict):
            self._spec_dict = {}
            for spec in X:
                self._spec_dict[spec] =\
                 type(self)(*self._initargs,
                  **self._initkwargs).fit(self._before_fit(atomic_shape(X[spec])))
            return self
        else:
            return super().fit(atomic_shape(X))


    def get_gradient(self, X,y=None, **fit_params):
        was_tuple = False
        if isinstance(X,tuple):
            y = X[1]
            X = X[0]
            was_tuple = True

        made_list = False
        if not isinstance(X, list):
            X = [X]
            made_list = True

        results = []
        for x in X:
            if isinstance(x, dict):
                results_dict = {}
                for spec in x:
                    results_dict[spec] = self._spec_dict[spec].get_gradient(x[spec])
                results.append(results_dict)
            else:
                results.append(system_shape(self._gradient_function(atomic_shape(x)),
                    x.shape[-2]))

        if made_list:
            results = results[0]
        if was_tuple:
            return results, y
        else:
            return results

    def fit_transform(self, X, y=None, **fit_params):
        return self.fit(X).transform(X)

# TODO: The better solution might be to have a factory, pass an instance of the object
# and copy this instance
class GroupedVarianceThreshold(GroupedTransformer, VarianceThreshold):

    def __init__(self, threshold=0.0):

        self._before_fit = identity # lambdas can't be pickled
        self._initargs = []
        self._initkwargs = dict(threshold=threshold)
        super().__init__(**self._initkwargs)

    def _gradient_function(self, X):
        X_shape = X.shape
        print('Var shape ', X.shape)
        if not X.ndim == 2:
            X = X.reshape(-1,X.shape[-1])

        support = self.get_support()
        X_grad = np.zeros([len(X),len(support)])
        X_grad[:, support] = X
        return X_grad.reshape(*X_shape[:-1], X_grad.shape[-1])

class GroupedPCA(GroupedTransformer, PCA):

    def __init__(self, n_components=None, copy=True, whiten=False, svd_solver='auto',
                 tol=0.0, iterated_power='auto', random_state=None):

        self._initkwargs = dict(n_components=n_components, copy=copy,
                 whiten=whiten, svd_solver=svd_solver,
                 tol=tol, iterated_power=iterated_power,
                 random_state=random_state)

        self._before_fit = StandardScaler().fit_transform
        self._initargs = []
        super().__init__(**self._initkwargs)

    def _gradient_function(self, X):
        X_shape = X.shape
        print('PCA shape', X.shape)
        if not X.ndim == 2:
            X = X.reshape(-1,X.shape[-1])
        X_grad =  X.dot(self.components_)
        return X_grad.reshape(*X_shape[:-1], X_grad.shape[-1])

def identity(x):
    return x
