import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from tqdm import trange

import tensorflow as tf
from . import warp, utils
from .regularizers import l2, curvature


class TWPCA(BaseEstimator, TransformerMixin):

    def __init__(self, n_components, shared_length=None,
                 trial_regularizer=l2(1e-6),
                 time_regularizer=l2(1e-6),
                 neuron_regularizer=l2(1e-6),
                 nonneg=False,
                 fit_trial_factors=False,
                 warptype='nonlinear',
                 warpinit='identity',
                 warp_regularizer=curvature(),
                 origin_idx=None):
        """Time-warped Principal Components Analysis

        Args:
            n_components: number of components to use
            shared_length (optional): length of each trial in the warped/shared space. If None,
                the length of the dataset is used (default: None)
            trial_regularizer (optional): regularization on the trial factors (default: l2(1e-6))
            time_regularizer (optional): regularization on the time factors (default: l2(1e-6))
            neuron_regularizer (optional): regularization on the neuron factors (default: l2(1e-6))
            nonneg (optional): whether latent factors are constrained to be nonnegative (default: False)
            fit_trial_factors (optional): whether or not to fit weights on each trial, in addition
                to the neuron and time factors (default: False)
            warptype: type of warps to allow ('nonlinear', 'affine', 'shift', or 'scale'). The
                default is 'nonlinear' which allows for full nonlinear warping
            warpinit: either 'identity', 'linear', 'shift', 'randn'
            warp_regularizer (optional): regularization on the warp function (default: curvature())
            origin_idx (optional): if not None, all warping functions are pinned (aligned) at this
                index. (default: None)
        """
        self.n_components = n_components
        self.shared_length = shared_length
        self.fit_trial_factors = fit_trial_factors
        self.warptype = warptype
        self.warpinit = warpinit
        self.origin_idx = origin_idx
        self.nonneg = nonneg

        # store regularization terms (these will match the variables in _params)
        self._regularizers = {
            'trial': trial_regularizer,
            'time': time_regularizer,
            'neuron': neuron_regularizer,
            'warp': warp_regularizer,
        }

        # store tensorflow variables, parameters and session
        self._raw_params = {} # tensorflow variables
        self._params = {}     # model parameters (if nonneg=True, softplus transform applied to raw_params)
        self._sess = None

    def fit(self, X, optimizer=tf.train.AdamOptimizer, niter=1000, lr=1e-3, sess=None, progressbar=True):
        """Fit the twPCA model

        Args:
            X: 3D numpy array with dimensions [n_trials, n_timepoints, n_neurons]
            optimizer (optional): a tf.train.Optimizer class (default: AdamOptimizer)
            niter (optional): number of iterations to run the optimizer for (default: 1000)
            sess (optional): tensorflow session to use for running the computation. If None,
                then a new session is created. (default: None)
            progressbar (optional): whether to print a progressbar (default: True)
        """

        # set the shared length to the number of timesteps if not already defined
        if self.shared_length is None:
            self.shared_length = X.shape[1]

        # convert niter and lr to iterables if given as scalars
        if (not np.iterable(niter)) and (not np.iterable(lr)):
            niter = (niter,)
            lr = (lr,)

        # niter and lr must have the same number of elements
        elif np.iterable(niter) and np.iterable(lr):
            niter = list(niter)
            lr = list(lr)
            if len(niter) != len(lr):
                raise ValueError("niter and lr must have the same length.")
        else:
            raise ValueError("niter and lr must either be numbers or iterables of the same length.")

        # Convert matrix to 3d
        np_X = np.atleast_3d(X.astype(np.float32))
        # Convet NaNs to 0 so TensorFlow doesn't throw NaNs in gradient
        # See: https://github.com/tensorflow/tensorflow/issues/2540
        self.X = tf.constant(np.nan_to_num(np_X))
        # pull out dimensions
        n_trials, n_timesteps, n_neurons = np_X.shape
        # Identify finite entries of data matrix
        np_mask = np.isfinite(X)
        self._num_datapoints = np.sum(np_mask)
        self._mask = tf.constant(np_mask, dtype=tf.float32)

        # Compute last non-nan index for each trial.
        # Note we find the first occurence of a non-nan in the reversed mask,
        # and then map that index back to the original mask
        rev_last_idx = np.argmax(np.all(np_mask, axis=-1)[:, ::-1], axis=1)
        self.last_idx = n_timesteps - rev_last_idx

        # build the parameterized warping functions
        self._params['warp'], self._inv_warp, warp_vars = warp.generate_warps(n_trials,
            n_timesteps, self.shared_length, self.warptype, self.warpinit, self.origin_idx, data=np_X, last_idx=self.last_idx)

        # Initialize factor matrices
        # TODO: compute low rank factors using initial warps, not raw data
        # TODO: rewrite so this works on NaNs
        trial_init, time_init, neuron_init = utils.compute_lowrank_factors(np_X, self.n_components, self.fit_trial_factors, self.nonneg, self.last_idx)

        # create tensorflow variables for factor matrices
        self._raw_params['time'] = tf.Variable(time_init, name="time_factors")
        self._raw_params['neuron'] = tf.Variable(neuron_init, name="neuron_factors")
        if self.fit_trial_factors:
            self._raw_params['trial'] = tf.Variable(trial_init, name="trial_factors")

        rectifier = tf.nn.softplus if self.nonneg else tf.identity
        self._params['time'] = rectifier(self._raw_params['time'])
        self._params['neuron'] = rectifier(self._raw_params['neuron'])
        if self.fit_trial_factors:
            self._params['trial'] = rectifier(self._raw_params['trial'])

        # warped time factors warped for each trial
        self._warped_time_factors = warp.warp(tf.tile(tf.expand_dims(self._params['time'], [0]), [n_trials, 1, 1]), self._params['warp'])

        if self.fit_trial_factors:
            # trial i, time j, factor k, neuron n
            self.X_pred = tf.einsum('ik,ijk,nk->ijn', self._params['trial'], self._warped_time_factors, self._params['neuron'])
        else:
            # trial i, time j, factor k, neuron n
            self.X_pred = tf.einsum('ijk,nk->ijn', self._warped_time_factors, self._params['neuron'])

        # total objective
        # only include terms that were not NaN in the original data matrix
        self.recon_cost = tf.reduce_sum(self._mask * (self.X_pred - self.X)**2) / self._num_datapoints
        self.objective = self.recon_cost + self.regularization
        self.obj_history = []

        # create a tensorflow session if necessary
        if sess is None:
            sess = tf.Session()
        self._sess = sess

        # create train_op
        self._lr = tf.placeholder(tf.float32, name="learning_rate")
        self._opt = optimizer(self._lr)
        var_list = [v for k, v in self._raw_params.items() if k != 'warp'] + list(warp_vars)
        self._train_op = self._opt.minimize(self.objective, var_list=var_list)

        # initialize variables
        utils.initialize_new_vars(self._sess)

        # run the optimizer
        for train_args in zip(niter, lr):
            self.train(*train_args, progressbar=progressbar)

        return self

    def train(self, niter, lr, progressbar=True):
        """Partially fits the model."""

        if progressbar:
            iterator = trange
        else:
            iterator = range

        ops = [self.objective, self._train_op]
        self.obj_history += [self._sess.run(ops, feed_dict={self._lr: lr})[0]
                             for tt in iterator(niter)]

    @property
    def params(self):
        """Returns a dictionary of factors and warps"""
        values = self._sess.run(list(self._params.values()))
        return dict(zip(self._params.keys(), values))

    def transform(self, X=None):
        """Transform the dataset from trial space into the shared space (de-jitters the raw data).

        Note: this uses the data that was used to initialize and fit the time parameters.

        Returns:
            [n_trials, shared_length, n_neurons] Tensor of data warped into shared space
        """
        if X is None:
            X_tf = self.X
        elif isinstance(X, np.ndarray):
            X_tf = tf.constant(X, dtype=tf.float32)
        elif isinstance(X, tf.Tensor):
            X_tf = X
        else:
            raise ValueError("X must be a numpy array or tensorflow tensor")

        return self._sess.run(warp.warp(X_tf, self._inv_warp))

    def predict(self, X=None):
        """Return model prediction of activity on each trial.

        Args:
            X (optional) : 3D numpy array with shape [n_trials, n_timepoints, n_neurons]

        Note: If `X` is not provided, the prediction of the model on training data
              (i.e. provided to `model.fit` function) is returned. If a new `X` is
              provided then it is assumed these are held-out neurons; in this case,
              `X` should have the same n_trials and n_timepoints as the training data
              provided to `model.fit`. The temporal factors and warps are re-used, and
              the neuron factors are newly fit in a least-squares sense.

        Returns:
            X_pred : 3D numpy array with shape [n_trials, n_timepoints, n_neurons] holding
                     low-dimensional model prediction.
        """
        if self._sess is None:
            raise ValueError('No model has been fit - must call TWPCA.fit() before TWPCA.predict().')

        if X is None:
            X_pred = self._sess.run(self.X_pred)
        elif isinstance(X, np.ndarray):
            # input is a (trial x time x neuron) dataset of unwarped data
            n_trials, n_timepoints, n_neurons = X.shape
            # grab the warped temporal factors
            if self.fit_trial_factors:
                warped_factors = self._sess.run(self._warped_time_factors)
                trial_factors = self._sess.run(self.self._params['trial'])
                warped_factors *= trial_factors[:, None, :] # broadcast multiply across trials
            else:
                warped_factors = self._sess.run(self._warped_time_factors)
            # check input size
            if warped_factors.shape[0] != n_trials:
                raise ValueError('Data does not have the expected number of trials.')
            if warped_factors.shape[1] != n_timepoints:
                raise ValueError('Data does not have the expected number of timepoints.')
            # reshape the factors and data into matrices
            # time factors is (trial-time x components); X_unf is (trial-time x neurons)
            time_factors = warped_factors.reshape(-1, self.n_components)
            X_unf = X.reshape(-1, n_neurons)
            # mask nan values (only evaluate when all neurons are recorded)
            mask = np.all(np.isfinite(X_unf), axis=-1)
            # do a least-squares solve to fit the neuron factors
            neuron_factors = np.linalg.lstsq(time_factors[mask, :], X_unf[mask, :])[0]
            # reconstruct and reshape the predicted activity
            X_pred = np.dot(neuron_factors.T, time_factors.T) # (neurons x trials-time)
            X_pred = X_pred.T.reshape(*X.shape) # (trials x time x neuron)

        return X_pred

    @property
    def regularization(self):
        """Computes the total regularization cost"""
        return sum(self._regularizers[key](param) for key, param in self._params.items())
