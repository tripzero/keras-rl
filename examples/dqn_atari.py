from __future__ import division
import argparse
import os

import cv2
from PIL import Image
import numpy as np
import gym

from keras.models import Sequential
from keras.layers import Dense, Activation, Flatten, Convolution2D, Permute
from keras.optimizers import Adam
import keras.backend as K

from rl.agents.dqn import DQNAgent
from rl.policy import LinearAnnealedPolicy, BoltzmannQPolicy, EpsGreedyQPolicy
from rl.memory import SequentialMemory
from rl.core import Processor
from rl.callbacks import FileLogger, ModelIntervalCheckpoint


INPUT_SHAPE = (84, 84)
WINDOW_LENGTH = 4

try:
    import ngraph_bridge
    import tensorflow as tf

    config = tf.compat.v1.ConfigProto()
    config_ngraph_enabled = ngraph_bridge.update_config(config)
    sess = tf.compat.v1.Session(config=config_ngraph_enabled)

    print(ngraph_bridge.list_backends())
    ngraph_bridge.set_backend('INTELGPU')

except Exception as ex:
    print(ex)
    import tensorflow as tf

    config = tf.compat.v1.ConfigProto()
    config.gpu_options.allow_growth = True
    config.gpu_options.per_process_gpu_memory_fraction = 2
    sess = tf.compat.v1.Session(config=config)


class AtariProcessor(Processor):

    def __init__(self, show=False, output_dir=None):
        self.show = show

        if show:
            self.out_file = os.path.join(output_dir, "atari_playback.avi")
            print("will write to {}".format(self.out_file))
            self.writer = None

    def process_observation(self, observation):
        assert observation.ndim == 3  # (height, width, channel)
        img = Image.fromarray(observation)
        img_p = img.resize(INPUT_SHAPE).convert(
            'L')  # resize and convert to grayscale
        processed_observation = np.array(img_p)

        if self.show:
            po = cv2.resize(np.array(img), (640, 480))
            if self.writer is None:
                codec = cv2.VideoWriter_fourcc('P', 'I', 'M', '1')
                self.writer = cv2.VideoWriter(
                    self.out_file, codec, 25, (po.shape[1], po.shape[0]))

            self.writer.write(po)

        assert processed_observation.shape == INPUT_SHAPE
        # saves storage in experience memory
        return processed_observation.astype('uint8')

    def process_state_batch(self, batch):
        # We could perform this processing step in `process_observation`. In this case, however,
        # we would need to store a `float32` array instead, which is 4x more memory intensive than
        # an `uint8` array. This matters if we store 1M observations.
        processed_batch = batch.astype('float32') / 255.
        return processed_batch

    def process_reward(self, reward):
        return np.clip(reward, -1., 1.)

    def finish(self):
        if self.show:
            print("finalizing video playback...")
            self.writer.release()


parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=['train', 'test'], default='train')
parser.add_argument('--env-name', type=str, default='BreakoutDeterministic-v4')
parser.add_argument('--weights', type=str, default=None)
parser.add_argument('--output_dir', type=str, default=None)
parser.add_argument('--show', action='store_true', default=False)
args = parser.parse_args()

# Get the environment and extract the number of actions.
env = gym.make(args.env_name)
np.random.seed(123)
env.seed(123)
nb_actions = env.action_space.n

# Next, we build our model. We use the same model that was described by
# Mnih et al. (2015).
input_shape = (WINDOW_LENGTH,) + INPUT_SHAPE
model = Sequential()
model.add(Permute((2, 3, 1), input_shape=input_shape))
model.add(Convolution2D(32, (8, 8), strides=(4, 4)))
model.add(Activation('relu'))
model.add(Convolution2D(64, (4, 4), strides=(2, 2)))
model.add(Activation('relu'))
model.add(Convolution2D(64, (3, 3), strides=(1, 1)))
model.add(Activation('relu'))
model.add(Flatten())
model.add(Dense(512))
model.add(Activation('relu'))
model.add(Dense(nb_actions))
model.add(Activation('linear'))
print(model.summary())

# Finally, we configure and compile our agent. You can use every built-in Keras optimizer and
# even the metrics!
memory = SequentialMemory(limit=500000, window_length=WINDOW_LENGTH)
processor = AtariProcessor(args.show, args.output_dir)

# Select a policy. We use eps-greedy action selection, which means that a random action is selected
# with probability eps. We anneal eps from 1.0 to 0.1 over the course of 1M steps. This is done so that
# the agent initially explores the environment (high eps) and then gradually sticks to what it knows
# (low eps). We also set a dedicated eps value that is used during testing. Note that we set it to 0.05
# so that the agent still performs some random actions. This ensures that
# the agent cannot get stuck.
policy = LinearAnnealedPolicy(EpsGreedyQPolicy(), attr='eps', value_max=1., value_min=.1, value_test=.05,
                              nb_steps=1000000)

# The trade-off between exploration and exploitation is difficult and an on-going research topic.
# If you want, you can experiment with the parameters or use a different policy. Another popular one
# is Boltzmann-style exploration:
# policy = BoltzmannQPolicy(tau=1.)
# Feel free to give it a try!

dqn = DQNAgent(model=model, nb_actions=nb_actions, policy=policy, memory=memory,
               processor=processor, nb_steps_warmup=50000, gamma=.99, target_model_update=10000,
               train_interval=4, delta_clip=1.)
dqn.compile(Adam(lr=.00025), metrics=['mae'])

if args.mode == 'train':
    # Okay, now it's time to learn something! We capture the interrupt exception so that training
    # can be prematurely aborted. Notice that now you can use the built-in
    # Keras callbacks!
    weights_filename = 'dqn_{}_weights.h5f'.format(args.env_name)

    checkpoint_weights_filename = 'dqn_' + \
        args.env_name + '_weights_{step}.h5f'
    log_filename = 'dqn_{}_log.json'.format(args.env_name)

    if args.output_dir is not None:
        checkpoint_weights_filename = os.path.join(
            args.output_dir, checkpoint_weights_filename)
        weights_filename = os.path.join(args.output_dir, weights_filename)
        log_filename = os.path.join(args.output_dir, log_filename)

    callbacks = [ModelIntervalCheckpoint(
        checkpoint_weights_filename, interval=250000)]
    callbacks += [FileLogger(log_filename, interval=100)]
    dqn.fit(env, callbacks=callbacks, nb_steps=1750000, log_interval=10000)

    # After training is done, we save the final weights one more time.
    dqn.save_weights(weights_filename, overwrite=True)
    processor.finish()

    # Finally, evaluate our algorithm for 10 episodes.
    # dqn.test(env, nb_episodes=10, visualize=False)
elif args.mode == 'test':
    log_filename = 'dqn_{}_log.json'.format(args.env_name)
    weights_filename = 'dqn_{}_weights.h5f'.format(args.env_name)

    if args.output_dir is not None:
        weights_filename = os.path.join(args.output_dir, weights_filename)
        log_filename = os.path.join(args.output_dir, log_filename)

    if args.weights:
        weights_filename = args.weights
    dqn.load_weights(weights_filename)
    dqn.test(env, nb_episodes=10, visualize=False)
