# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Compile Tensorflow Models
=========================
This article is an introductory tutorial to deploy tensorflow models with TVM.

For us to begin with, tensorflow python module is required to be installed.

Please refer to https://www.tensorflow.org/install
"""

# tvm, relay
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

import tvm
from tvm import relay

# os and numpy
import numpy as np
import os.path

# Tensorflow imports
import tensorflow as tf

# Tensorflow utility functions
import tvm.relay.testing.tf as tf_testing

from tvm import autotvm
from tvm.autotvm.tuner import XGBTuner, GATuner, RandomTuner, GridSearchTuner
from tvm.contrib.util import tempdir

# Base location for model related files.
repo_base = 'https://github.com/dmlc/web-data/raw/master/tensorflow/models/InceptionV1/'

# Test image
img_name = 'elephant-299.jpg'
image_url = os.path.join(repo_base, img_name)

######################################################################
# Tutorials
# ---------
# Please refer docs/frontend/tensorflow.md for more details for various models
# from tensorflow.

model_name = 'classify_image_graph_def-with_shapes.pb'
model_url = os.path.join(repo_base, model_name)

# Image label map
map_proto = 'imagenet_2012_challenge_label_map_proto.pbtxt'
map_proto_url = os.path.join(repo_base, map_proto)

# Human readable text for labels
label_map = 'imagenet_synset_to_human_label_map.txt'
label_map_url = os.path.join(repo_base, label_map)

# Target settings
# Use these commented settings to build for cuda.
#target = 'cuda'
#target_host = 'llvm'
#layout = "NCHW"
#ctx = tvm.gpu(0)
#target = 'llvm'
target = 'llvm -mcpu=core-avx2'
target_host = 'llvm -mcpu=core-avx2'
target_host = 'llvm'
layout = None
ctx = tvm.cpu(0)

######################################################################
# Download required files
# -----------------------
# Download files listed above.
from tvm.contrib.download import download_testdata

img_path = download_testdata(image_url, img_name, module='data')
model_path = download_testdata(model_url, model_name, module=['tf', 'InceptionV1'])
map_proto_path = download_testdata(map_proto_url, map_proto, module='data')
label_path = download_testdata(label_map_url, label_map, module='data')

######################################################################
# Import model
# ------------
# Creates tensorflow graph definition from protobuf file.

with tf.gfile.FastGFile(model_path, 'rb') as f:
    graph_def = tf.GraphDef()
    graph_def.ParseFromString(f.read())
    graph = tf.import_graph_def(graph_def, name='')
    # Call the utility to import the graph definition into default graph.
    graph_def = tf_testing.ProcessGraphDefParam(graph_def)
    # Add shapes to the graph.
    with tf.Session() as sess:
        graph_def = tf_testing.AddShapesToGraphDef(sess, 'softmax')

###################################################################
# Begin Tuning
# ------------
# Now we can extract tuning tasks from the network and begin tuning.
# Here, we provide a simple utility function to tune a list of tasks.
# This function is just an initial implementation which tunes them in sequential order.
# We will introduce a more sophisticated tuning scheduler in the future.

# You can skip the implementation of this function for this tutorial.
def tune_tasks(tasks,
               measure_option,
               tuner='xgb',
               n_trial=1000,
               early_stopping=None,
               log_filename='tuning.log',
               use_transfer_learning=True):
               
    # create tmp log file
    tmp_log_file = log_filename + ".tmp"
    if os.path.exists(tmp_log_file):
        os.remove(tmp_log_file)

    for i, tsk in enumerate(reversed(tasks)):
        prefix = "[Task %2d/%2d] " %(i+1, len(tasks))

        # create tuner
        if tuner == 'xgb' or tuner == 'xgb-rank':
            tuner_obj = XGBTuner(tsk, loss_type='rank')
        elif tuner == 'ga':
            tuner_obj = GATuner(tsk, pop_size=100)
        elif tuner == 'random':
            tuner_obj = RandomTuner(tsk)
        elif tuner == 'gridsearch':
            tuner_obj = GridSearchTuner(tsk)
        else:
            raise ValueError("Invalid tuner: " + tuner)

        if use_transfer_learning:
            if os.path.isfile(tmp_log_file):
                tuner_obj.load_history(autotvm.record.load_from_file(tmp_log_file))

        # do tuning
        tuner_obj.tune(n_trial=min(n_trial, len(tsk.config_space)),
                       early_stopping=early_stopping,
                       measure_option=measure_option,
                       callbacks=[
                           autotvm.callback.progress_bar(n_trial, prefix=prefix),
                           autotvm.callback.log_to_file(tmp_log_file)])

    # pick best records to a cache file
    autotvm.record.pick_best(tmp_log_file, log_filename)
    os.remove(tmp_log_file)



######################################################################
# Decode image
# ------------
# .. note::
#
#   tensorflow frontend import doesn't support preprocessing ops like JpegDecode.
#   JpegDecode is bypassed (just return source node).
#   Hence we supply decoded frame to TVM instead.
#

from PIL import Image
image = Image.open(img_path).resize((299, 299))

x = np.array(image)

######################################################################
# Import the graph to Relay
# -------------------------
# Import tensorflow graph definition to relay frontend.
#
# Results:
#   sym: relay expr for given tensorflow protobuf.
#   params: params converted from tensorflow params (tensor protobuf).
shape_dict = {'DecodeJpeg/contents': x.shape}
dtype_dict = {'DecodeJpeg/contents': 'uint8'}
mod, params = relay.frontend.from_tensorflow(graph_def,
                                             layout=layout,
                                             shape=shape_dict)
print("==========",type(mod))
print("==========",type(mod['main']))
print("==========",mod['main'].astext(show_meta_data=False))
net = mod['main']
with relay.build_config(opt_level=3):
    qfunc = relay.quantize.prerequisite_optimize(net, params=params)

print("=======",qfunc.astext(show_meta_data=False))

# os._exit(-1)
tasks = autotvm.task.extract_from_program(qfunc, target=target,
                                            params=params, ops=(relay.op.nn.conv2d,))
for i in range(len(tasks)):
    op_name = tasks[i].workload[0]
    if op_name == 'conv2d':
        func_create = 'topi_x86_conv2d_NCHWc'
    elif op_name == 'depthwise_conv2d_nchw':
        func_create = 'topi_x86_depthwise_conv2d_NCHWc_from_nchw'
    else:
        print ("Tuning {} is not supported on x86")
        raise ValueError("Tuning {} is not supported on x86".format(op_name))

    print ( "[Create Task %2d/%2d (%s, %s) ] " % (i+1, len(tasks), tasks[i].name, tasks[i].workload[0]))

    tsk = autotvm.task.create(func_create, args=tasks[i].args,
                                target=tasks[i].target, template_key='direct')
    print("================",type(tsk),tsk)
    tsk.workload = tasks[i].workload
    tasks[i] = tsk

# run tuning tasks
# logging.info("Tuning...",type(tasks))
# os._exit(-1)

tune_tasks(tasks, **tuning_opt)



print("Tensorflow protobuf imported to relay frontend.")
######################################################################
# Relay Build
# -----------
# Compile the graph to llvm target with given input specification.
#
# Results:
#   graph: Final graph after compilation.
#   params: final params after compilation.
#   lib: target library which can be deployed on target with TVM runtime.

with relay.build_config(opt_level=3):
    graph, lib, params = relay.build(mod,
                                     target=target,
                                     target_host=target_host,
                                     params=params)

######################################################################
# Execute the portable graph on TVM
# ---------------------------------
# Now we can try deploying the compiled model on target.

from tvm.contrib import graph_runtime
dtype = 'uint8'
m = graph_runtime.create(graph, lib, ctx)
# set inputs
m.set_input('DecodeJpeg/contents', tvm.nd.array(x.astype(dtype)))
m.set_input(**params)
# execute
import time
start = time.time()
m.run()
print("==================", time.time()-start)
# get outputs
tvm_output = m.get_output(0, tvm.nd.empty(((1, 1008)), 'float32'))

######################################################################
# Process the output
# ------------------
# Process the model output to human readable text for InceptionV1.
predictions = tvm_output.asnumpy()
predictions = np.squeeze(predictions)

# Creates node ID --> English string lookup.
node_lookup = tf_testing.NodeLookup(label_lookup_path=map_proto_path,
                                    uid_lookup_path=label_path)

# Print top 5 predictions from TVM output.
top_k = predictions.argsort()[-5:][::-1]
for node_id in top_k:
    human_string = node_lookup.id_to_string(node_id)
    score = predictions[node_id]
    print('%s (score = %.5f)' % (human_string, score))

######################################################################
# Inference on tensorflow
# -----------------------
# Run the corresponding model on tensorflow

def create_graph():
    """Creates a graph from saved GraphDef file and returns a saver."""
    # Creates graph from saved graph_def.pb.
    with tf.gfile.FastGFile(model_path, 'rb') as f:
        graph_def = tf.GraphDef()
        graph_def.ParseFromString(f.read())
        graph = tf.import_graph_def(graph_def, name='')
        # Call the utility to import the graph definition into default graph.
        graph_def = tf_testing.ProcessGraphDefParam(graph_def)

def run_inference_on_image(image):
    """Runs inference on an image.

    Parameters
    ----------
    image: String
        Image file name.

    Returns
    -------
        Nothing
    """
    if not tf.gfile.Exists(image):
        tf.logging.fatal('File does not exist %s', image)
    image_data = tf.gfile.FastGFile(image, 'rb').read()

    # Creates graph from saved GraphDef.
    create_graph()

    with tf.Session() as sess:
        softmax_tensor = sess.graph.get_tensor_by_name('softmax:0')
        
        start = time.time()
        predictions = sess.run(softmax_tensor,
                               {'DecodeJpeg/contents:0': image_data})
        print("==================", time.time()-start)
        predictions = np.squeeze(predictions)

        # Creates node ID --> English string lookup.
        node_lookup = tf_testing.NodeLookup(label_lookup_path=map_proto_path,
                                            uid_lookup_path=label_path)

        # Print top 5 predictions from tensorflow.
        top_k = predictions.argsort()[-5:][::-1]
        print ("===== TENSORFLOW RESULTS =======")
        for node_id in top_k:
            human_string = node_lookup.id_to_string(node_id)
            score = predictions[node_id]
            print('%s (score = %.5f)' % (human_string, score))

run_inference_on_image(img_path)
