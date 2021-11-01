#!/usr/bin/env python
"""Demonstrate basic API of plot_clusterer.
---
id: 0.sklearn.plot_clusterer-basic
plugin:
    - wandb
depend:
    requirements:
        - numpy
        - scikit-learn
assert:
    - :wandb:runs_len: 1
    - :wandb:runs[0][exitcode]: 0
    - :yea:exit: 0
"""
import numpy as np
from sklearn import datasets
from sklearn.cluster import KMeans
import wandb

wandb.init("my-scikit-integration")

iris = datasets.load_iris()
X, y = iris.data, iris.target

names = iris.target_names
labels = np.array([names[target] for target in y])

kmeans = KMeans(n_clusters=4, random_state=1)

cluster_labels = kmeans.fit_predict(X)

wandb.sklearn.plot_clusterer(kmeans, X, cluster_labels, labels, 'KMeans')
