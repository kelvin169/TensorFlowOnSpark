# Copyright 2017 Yahoo Inc.
# Licensed under the terms of the Apache 2.0 license.
# Please see LICENSE file in the project root for terms.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.ml.param.shared import *
from pyspark.ml.pipeline import Estimator, Model, Pipeline
from pyspark.sql import SparkSession

import argparse
import os
import numpy
import sys
import tensorflow as tf
import threading
import time
from datetime import datetime

from tensorflowonspark import TFCluster
import mnist
import mnist_dist

sc = SparkContext(conf=SparkConf().setAppName("mnist_spark"))
spark = SparkSession(sc)

executors = sc._conf.get("spark.executor.instances")
num_executors = int(executors) if executors is not None else 1
num_ps = 1

parser = argparse.ArgumentParser()
parser.add_argument("-b", "--batch_size", help="number of records per batch", type=int, default=100)
parser.add_argument("-e", "--epochs", help="number of epochs", type=int, default=1)
parser.add_argument("-f", "--format", help="example format: (csv|pickle|tfr)", choices=["csv","pickle","tfr"], default="csv")
parser.add_argument("-i", "--images", help="HDFS path to MNIST images in parallelized format")
parser.add_argument("-l", "--labels", help="HDFS path to MNIST labels in parallelized format")
parser.add_argument("-m", "--model", help="HDFS path to save/load model during train/inference", default="mnist_model")
parser.add_argument("-n", "--cluster_size", help="number of nodes in the cluster", type=int, default=num_executors)
parser.add_argument("-o", "--output", help="HDFS path to save test/inference output", default="predictions")
parser.add_argument("-r", "--readers", help="number of reader/enqueue threads", type=int, default=1)
parser.add_argument("-s", "--steps", help="maximum number of steps", type=int, default=1000)
parser.add_argument("-tb", "--tensorboard", help="launch tensorboard process", action="store_true")
parser.add_argument("-X", "--mode", help="train|inference", default="train")
parser.add_argument("-c", "--rdma", help="use rdma connection", default=False)
args = parser.parse_args()
print("args:",args)

class HasArgs(Params):
  args = Param(Params._dummy(), "args", "args", typeConverter=TypeConverters.toListString)
  def __init__(self):
    super(HasArgs, self).__init__()
  def setArgs(self, value):
    return self._set(args=value)
  def getArgs(self):
    return self.getOrDefault(self.args)

class TFEstimator(Estimator, HasArgs):
  def _fit(self, dataset):
    args = parser.parse_args(self.getArgs())
    args.mode = 'train'
    print("===== train args: {0}".format(args))
    cluster = TFCluster.run(sc, mnist_dist.map_fun, args, args.cluster_size, num_ps, args.tensorboard, TFCluster.InputMode.SPARK)
    cluster.train(dataset.rdd, args.epochs)
    cluster.shutdown()
    return TFModel().setArgs(self.getArgs())

class TFModel(Model, HasArgs):
  def _transform(self, dataset):
    args = parser.parse_args(self.getArgs())
    args.mode = 'inference'
    print("===== inference args: {0}".format(args))
#    cluster = TFCluster.run(sc, mnist_dist.map_fun, args, args.cluster_size, num_ps, args.tensorboard, TFCluster.InputMode.SPARK)
#    preds = cluster.inference(dataset.rdd)
#    # cluster.shutdown()
#    result = spark.createDataFrame(preds, "string")
#    return result
    rdd_out = dataset.rdd.mapPartitions(lambda it: mnist.map_fun(args, it))
    return spark.createDataFrame(rdd_out, "string")

print("{0} ===== Start".format(datetime.now().isoformat()))

if args.format == "tfr":
  images = sc.newAPIHadoopFile(args.images, "org.tensorflow.hadoop.io.TFRecordFileInputFormat",
                              keyClass="org.apache.hadoop.io.BytesWritable",
                              valueClass="org.apache.hadoop.io.NullWritable")
  def toNumpy(bytestr):
    example = tf.train.Example()
    example.ParseFromString(bytestr)
    features = example.features.feature
    image = numpy.array(features['image'].int64_list.value)
    label = numpy.array(features['label'].int64_list.value)
    return (image, label)
  dataRDD = images.map(lambda x: toNumpy(str(x[0])))
else:
  if args.format == "csv":
    images = sc.textFile(args.images).map(lambda ln: [int(x) for x in ln.split(',')])
    labels = sc.textFile(args.labels).map(lambda ln: [float(x) for x in ln.split(',')])
  else: # args.format == "pickle":
    images = sc.pickleFile(args.images)
    labels = sc.pickleFile(args.labels)
  print("zipping images and labels")
  dataRDD = images.zip(labels)

# Pipeline API
df = spark.createDataFrame(dataRDD)

print("{0} ===== Estimator.fit()".format(datetime.now().isoformat()))
estimator = TFEstimator().setArgs(sys.argv[1:])
model = estimator.fit(df)

print("{0} ===== Model.transform()".format(datetime.now().isoformat()))
#model = TFModel().setArgs(sys.argv[1:])
preds = model.transform(df)
preds.write.text(args.output)

print("{0} ===== Stop".format(datetime.now().isoformat()))

