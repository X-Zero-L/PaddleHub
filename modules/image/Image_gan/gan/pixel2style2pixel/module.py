# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import argparse
import copy

import paddle
import paddlehub as hub
from paddlehub.module.module import moduleinfo, runnable, serving
import numpy as np
import cv2
from skimage.io import imread
from skimage.transform import rescale, resize

from .model import Pixel2Style2PixelPredictor
from .util import base64_to_cv2


@moduleinfo(
    name="pixel2style2pixel",
    type="CV/style_transfer",
    author="paddlepaddle",
    author_email="",
    summary="",
    version="1.0.0")
class pixel2style2pixel:
    def __init__(self):
        self.pretrained_model = os.path.join(self.directory, "pSp-ffhq-inversion.pdparams")

        self.network = Pixel2Style2PixelPredictor(weight_path=self.pretrained_model, model_type='ffhq-inversion')

    def style_transfer(self,
                       images=None,
                       paths=None,
                       output_dir='./transfer_result/',
                       use_gpu=False,
                       visualization=True):
        '''


        images (list[numpy.ndarray]): data of images, shape of each is [H, W, C], color space must be BGR(read by cv2).
        paths (list[str]): paths to images
        output_dir: the dir to save the results
        use_gpu: if True, use gpu to perform the computation, otherwise cpu.
        visualization: if True, save results in output_dir.
        '''
        results = []
        paddle.disable_static()
        place = 'gpu:0' if use_gpu else 'cpu'
        place = paddle.set_device(place)
        if images is None and paths is None:
            print('No image provided. Please input an image or a image path.')
            return

        if images != None:
            for image in images:
                image = image[:, :, ::-1]
                out = self.network.run(image)
                results.append(out)

        if paths != None:
            for path in paths:
                image = cv2.imread(path)[:, :, ::-1]
                out = self.network.run(image)
                results.append(out)

        if visualization == True:
            if not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)
            for i, out in enumerate(results):
                if out is not None:
                    cv2.imwrite(os.path.join(output_dir, f'output_{i}.png'), out[0][:, :, ::-1])
                    np.save(os.path.join(output_dir, f'output_{i}.npy'), out[1])

        return results

    @runnable
    def run_cmd(self, argvs: list):
        """
        Run as a command.
        """
        self.parser = argparse.ArgumentParser(
            description=f"Run the {self.name} module.",
            prog=f'hub run {self.name}',
            usage='%(prog)s',
            add_help=True,
        )

        self.arg_input_group = self.parser.add_argument_group(title="Input options", description="Input data. Required")
        self.arg_config_group = self.parser.add_argument_group(
            title="Config options", description="Run configuration for controlling module behavior, not required.")
        self.add_module_config_arg()
        self.add_module_input_arg()
        self.args = self.parser.parse_args(argvs)
        return self.style_transfer(
            paths=[self.args.input_path],
            output_dir=self.args.output_dir,
            use_gpu=self.args.use_gpu,
            visualization=self.args.visualization,
        )

    @serving
    def serving_method(self, images, **kwargs):
        """
        Run as a service.
        """
        images_decode = [base64_to_cv2(image) for image in images]
        results = self.style_transfer(images=images_decode, **kwargs)
        return [result.tolist() for result in results]

    def add_module_config_arg(self):
        """
        Add the command config options.
        """
        self.arg_config_group.add_argument('--use_gpu', action='store_true', help="use GPU or not")

        self.arg_config_group.add_argument(
            '--output_dir', type=str, default='transfer_result', help='output directory for saving result.')
        self.arg_config_group.add_argument('--visualization', type=bool, default=False, help='save results or not.')

    def add_module_input_arg(self):
        """
        Add the command input options.
        """
        self.arg_input_group.add_argument('--input_path', type=str, help="path to input image.")
