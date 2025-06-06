# Copyright (c) 2021 Horizon Robotics and ALF Contributors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""This file contains basic configurations for PPO. An entropy target is
automatically enforced for PPO's policy.
"""

import alf
from alf.algorithms.agent import Agent
from alf.algorithms.ppo_algorithm import PPOAlgorithm, PPOLoss

alf.config(
    'Agent', rl_algorithm_cls=PPOAlgorithm, enforce_entropy_target=False)

alf.config('EntropyTargetAlgorithm', initial_alpha=1.)

alf.config('PPOLoss', entropy_regularization=None, normalize_advantages=True)

alf.config('PPOAlgorithm', loss_class=PPOLoss)

alf.config(
    'TrainerConfig',
    algorithm_ctor=Agent,
    whole_replay_buffer_training=True,
    clear_replay_buffer=True)
