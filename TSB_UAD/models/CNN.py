from __future__ import division
from __future__ import print_function

import numpy as np
import torch
from sklearn.utils import check_array
from sklearn.utils.validation import check_is_fitted
from torch import nn
from sklearn.preprocessing import MinMaxScaler
from .base import BaseDetector
from ..utils.stat_models import pairwise_distances_no_broadcast
from ..utils.utility import get_activation_by_name
from ..utils.dataset import TSDataset_Pred

class AdaptiveConcatPool1d(nn.Module):
    def __init__(self):
        super().__init__()
        self.ap = torch.nn.AdaptiveAvgPool1d(1)
        self.mp = torch.nn.AdaptiveAvgPool1d(1)
    
    def forward(self, x):
        return torch.cat([self.ap(x), self.mp(x)], 1)

class InnerCNN(nn.Module):
    def __init__(self,
                 n_features,
                 num_channel=(32, 32, 40),
                 kernel_size=3,
                 stride=1,
                 predict_time_steps=1,
                 dropout_rate=0.25,
                 hidden_activation='relu'):

        # initialize the super class
        super(InnerCNN, self).__init__()

        # save the default values
        self.n_features = n_features
        self.dropout_rate = dropout_rate
        self.hidden_activation = hidden_activation
        self.kernel_size = kernel_size
        self.stride = stride
        self.predict_time_steps = predict_time_steps
        self.num_channel = num_channel

        # get the object for the activations functions
        self.activation = get_activation_by_name(hidden_activation)

        # initialize encoder and decoder as a sequential
        self.conv_layers = nn.Sequential()
        prev_channels = 1

        for idx, out_channels in enumerate(self.num_channel[:-1]):
            self.conv_layers.add_module(
                "conv" + str(idx),
                torch.nn.Conv1d(prev_channels, self.num_channel[idx + 1], 
                self.kernel_size, self.stride))
            self.conv_layers.add_module(self.hidden_activation + str(idx),
                                    self.activation)
            self.conv_layers.add_module("pool" + str(idx), nn.MaxPool1d(kernel_size=2))
            prev_channels = out_channels

        self.fc = nn.Sequential(
            AdaptiveConcatPool1d(),
            torch.nn.Flatten(),
            torch.nn.Linear(2*self.num_channel[-1], self.num_channel[-1]),
            torch.nn.ReLU(),
            torch.nn.Dropout(dropout_rate),
            torch.nn.Linear(self.num_channel[-1], self.predict_time_steps)
        )

    def forward(self, x):
        b, c = x.shape
        x = x.view(b, 1, c)
        x = self.conv_layers(x)

        x = self.fc(x)      # (batch, feature, predict_time_steps)
        return x

class CNN(BaseDetector):

    def __init__(self,
                 slidingWindow=100,
                 predict_time_steps=1,
                 num_channel=None,
                 kernel_size=2,
                 stride=1,
                 hidden_activation='relu',
                 learning_rate=1e-3,
                 epochs=100,
                 batch_size=32,
                 dropout_rate=0.2,
                 weight_decay=1e-5,
                 # validation_size=0.1,
                 preprocessing=False,
                 loss_fn=None,
                 verbose=False,
                 # random_state=None,
                 contamination=0.1,
                 device=None):
        super(CNN, self).__init__(contamination=contamination)

        # save the initialization values
        self.slidingWindow = slidingWindow
        self.predict_time_steps = predict_time_steps
        self.num_channel = num_channel
        self.hidden_activation = hidden_activation
        self.kernel_size = kernel_size
        self.stride = stride
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.batch_size = batch_size
        self.dropout_rate = dropout_rate
        self.weight_decay = weight_decay
        self.preprocessing = preprocessing
        self.loss_fn = loss_fn
        self.verbose = verbose
        self.device = device

        # create default loss functions
        if self.loss_fn is None:
            self.loss_fn = torch.nn.MSELoss()

        # create default calculation device (support GPU if available)
        if self.device is None:
            self.device = torch.device(
                "cuda:0" if torch.cuda.is_available() else "cpu")

        if self.num_channel is None:
            self.num_channel = [32, 32, 64]

    # noinspection PyUnresolvedReferences
    def fit(self, X, y=None):
        """Fit detector. y is ignored in unsupervised methods.

        Parameters
        ----------
        X : numpy array of shape (n_samples, n_features)
            The input samples.

        y : Ignored
            Not used, present for API consistency by convention.

        Returns
        -------
        self : object
            Fitted estimator.
        """
        X_win, y_win = self.create_dataset(X, self.slidingWindow, self.predict_time_steps)
        
        # validate inputs X and y (optional)
        X_win = check_array(X_win)
        X_win = MinMaxScaler(feature_range=(0,1)).fit_transform(X_win.T).T

        # conduct standardization if needed
        if self.preprocessing:
            self.mean, self.std = np.mean(X_win, axis=0), np.std(X_win, axis=0)
            train_set = TSDataset_Pred(X=X_win, y=y_win, mean=self.mean, std=self.std)
        else:
            train_set = TSDataset_Pred(X=X_win, y=y_win)

        train_loader = torch.utils.data.DataLoader(train_set, batch_size=self.batch_size, shuffle=True)

        # initialize the model
        self.model = InnerCNN(
            n_features=self.slidingWindow,
            num_channel=self.num_channel)

        # move to device and print model information
        self.model = self.model.to(self.device)
        if self.verbose:
            print(self.model)

        # train the autoencoder to find the best one
        self._train_cnn(train_loader)

        self.model.load_state_dict(self.best_model_dict)
        self.decision_scores_ = self.decision_function(X)

        self._process_decision_scores()
        return self

    def _train_cnn(self, train_loader):
        """Internal function to train the CNN model

        Parameters
        ----------
        train_loader : torch dataloader
            Train data.
        """
        optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.learning_rate,
            weight_decay=self.weight_decay)

        self.best_loss = float('inf')
        self.best_model_dict = None

        for epoch in range(self.epochs):
            overall_loss = []
            for historical_data, target, idx in train_loader:
                historical_data = historical_data.to(self.device).float()
                target = target.to(self.device).float()
                loss = self.loss_fn(target, self.model(historical_data))

                self.model.zero_grad()
                loss.backward()
                optimizer.step()
                overall_loss.append(loss.item())
            if self.verbose:
                print('epoch {epoch}: training loss {train_loss} '.format(
                    epoch=epoch, train_loss=np.mean(overall_loss)))

            # track the best model so far
            if np.mean(overall_loss) <= self.best_loss:
                # print("epoch {ep} is the current best; loss={loss}".format(ep=epoch, loss=np.mean(overall_loss)))
                self.best_loss = np.mean(overall_loss)
                self.best_model_dict = self.model.state_dict()

    def create_dataset(self, X, slidingWindow, predict_time_steps=1):
        Xs, ys = [], []
        for i in range(len(X) - slidingWindow - predict_time_steps+1):
            tmp = X[i : i + slidingWindow + predict_time_steps]
            tmp= MinMaxScaler(feature_range=(0,1)).fit_transform(tmp.reshape(-1,1)).ravel()
            x = tmp[:slidingWindow]
            y = tmp[slidingWindow:]
            Xs.append(x)
            ys.append(y)
        return np.array(Xs), np.array(ys)

    def decision_function(self, X, measure=None):
        """Derive the decision score based on the given distance measure
        Parameters
        ----------
        X : numpy array of shape (n_samples, )
            The input samples.
        measure : object
            object for given distance measure with methods to derive the score
        Returns
        -------
        self : object
            Fitted estimator.
        """
        check_is_fitted(self, ['model', 'best_model_dict'])

        n_samples, n_features = X.shape[0], X.shape[1]

        if n_features == 1:
            X, y = self.create_dataset(X, self.slidingWindow, self.predict_time_steps)

        X = check_array(X)
        X = MinMaxScaler(feature_range=(0,1)).fit_transform(X.T).T
        # note the shuffle may be true but should be False
        if self.preprocessing:
            dataset = TSDataset_Pred(X=X, y=y, mean=self.mean, std=self.std)
        else:
            dataset = TSDataset_Pred(X=X, y=y)

        dataloader = torch.utils.data.DataLoader(dataset,
                                                 batch_size=self.batch_size,
                                                 shuffle=False)
        # enable the evaluation mode
        self.model.eval()

        # construct the vector for holding the reconstruction error
        outlier_scores = np.zeros([X.shape[0], ])
        
        with torch.no_grad():
            for historical_data, target, idx in dataloader:
                historical_data = historical_data.to(self.device).float()
                if measure is None:
                    outlier_scores[idx] = pairwise_distances_no_broadcast(target, self.model(historical_data).cpu().numpy())
                else:
                    outlier_scores[idx] = measure.measure(target, self.model(historical_data).cpu().numpy(), 0)
        
        # padded decision scores
        if outlier_scores.shape[0] < n_samples:
            padded_decision_scores_ = np.zeros(n_samples)
            padded_decision_scores_[: self.slidingWindow//2] = outlier_scores[0]
            padded_decision_scores_[self.slidingWindow//2 : self.slidingWindow//2+X.shape[0]]=outlier_scores
            padded_decision_scores_[self.slidingWindow//2+X.shape[0]:]=outlier_scores[-1]
        
        return padded_decision_scores_