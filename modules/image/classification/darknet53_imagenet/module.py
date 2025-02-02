import os
import ast
import argparse

import numpy as np
import paddle.fluid as fluid
import paddlehub as hub
from paddlehub.module.module import moduleinfo, runnable
from paddle.fluid.core import PaddleTensor, AnalysisConfig, create_paddle_predictor
from paddlehub.common.paddle_helper import add_vars_prefix
from paddlehub.io.parser import txt_parser

from darknet53_imagenet.darknet import DarkNet
from darknet53_imagenet.processor import load_label_info
from darknet53_imagenet.data_feed import test_reader


@moduleinfo(
    name="darknet53_imagenet",
    version="1.1.0",
    type="cv/classification",
    summary="DarkNet53 is a image classfication model trained with ImageNet-2012 dataset.",
    author="paddlepaddle",
    author_email="paddle-dev@baidu.com")
class DarkNet53(hub.Module):
    def _initialize(self):
        self.default_pretrained_model_path = os.path.join(self.directory, "darknet53_model")
        self.label_names = load_label_info(os.path.join(self.directory, "label_file.txt"))
        self.infer_prog = None
        self.pred_out = None
        self._set_config()

    def get_expected_image_width(self):
        return 224

    def get_expected_image_height(self):
        return 224

    def get_pretrained_images_mean(self):
        return np.array([0.485, 0.456, 0.406]).reshape(1, 3)

    def get_pretrained_images_std(self):
        return np.array([0.229, 0.224, 0.225]).reshape(1, 3)

    def _set_config(self):
        """
        predictor config setting
        """
        cpu_config = AnalysisConfig(self.default_pretrained_model_path)
        cpu_config.disable_glog_info()
        cpu_config.disable_gpu()
        self.cpu_predictor = create_paddle_predictor(cpu_config)

        try:
            _places = os.environ["CUDA_VISIBLE_DEVICES"]
            int(_places[0])
            use_gpu = True
        except:
            use_gpu = False
        if use_gpu:
            gpu_config = AnalysisConfig(self.default_pretrained_model_path)
            gpu_config.disable_glog_info()
            gpu_config.enable_use_gpu(memory_pool_init_size_mb=500, device_id=0)
            self.gpu_predictor = create_paddle_predictor(gpu_config)

    def context(self, input_image=None, trainable=True, pretrained=True, param_prefix='', get_prediction=False):
        """Distill the Head Features, so as to perform transfer learning.

        :param input_image: image tensor.
        :type input_image: <class 'paddle.fluid.framework.Variable'>
        :param trainable: whether to set parameters trainable.
        :type trainable: bool
        :param pretrained: whether to load default pretrained model.
        :type pretrained: bool
        :param param_prefix: the prefix of parameters in yolo_head and backbone
        :type param_prefix: str
        :param get_prediction: whether to get prediction,
            if True, outputs is {'bbox_out': bbox_out},
            if False, outputs is {'head_features': head_features}.
        :type get_prediction: bool
        """
        context_prog = input_image.block.program if input_image else fluid.Program()
        startup_program = fluid.Program()
        with fluid.program_guard(context_prog, startup_program):
            image = input_image or fluid.data(
                name='image', shape=[-1, 3, 224, 224], dtype='float32', lod_level=0
            )
            backbone = DarkNet(get_prediction=get_prediction)
            out = backbone(image)
            inputs = {'image': image}
            outputs = {'pred_out': out} if get_prediction else {'body_feats': out}
            place = fluid.CPUPlace()
            exe = fluid.Executor(place)
            if pretrained:

                def _if_exist(var):
                    return os.path.exists(os.path.join(self.default_pretrained_model_path, var.name))

                if not param_prefix:
                    fluid.io.load_vars(
                        exe, self.default_pretrained_model_path, main_program=context_prog, predicate=_if_exist)
            else:
                exe.run(startup_program)
            return inputs, outputs, context_prog

    def classification(self, paths=None, images=None, use_gpu=False, batch_size=1, top_k=2):
        """API of Classification.
        :param paths: the path of images.
        :type paths: list, each element is correspond to the path of an image.
        :param images: data of images, [N, H, W, C]
        :type images: numpy.ndarray
        :param use_gpu: whether to use gpu or not.
        :type use_gpu: bool
        :param batch_size: batch size.
        :type batch_size: int
        :param top_k : top k
        :type top_k : int
        """
        if self.infer_prog is None:
            inputs, outputs, self.infer_prog = self.context(trainable=False, pretrained=True, get_prediction=True)
            self.infer_prog = self.infer_prog.clone(for_test=True)
            self.pred_out = outputs['pred_out']
        place = fluid.CUDAPlace(0) if use_gpu else fluid.CPUPlace()
        exe = fluid.Executor(place)
        paths = paths or []
        all_images = list(test_reader(paths, images))
        images_num = len(all_images)
        loop_num = int(np.ceil(images_num / batch_size))

        res_list = []
        top_k = max(min(top_k, 1000), 1)
        for iter_id in range(loop_num):
            batch_data = []
            handle_id = iter_id * batch_size
            for image_id in range(batch_size):
                try:
                    batch_data.append(all_images[handle_id + image_id])
                except:
                    pass
            batch_data = np.array(batch_data).astype('float32')
            data_tensor = PaddleTensor(batch_data.copy())
            if use_gpu:
                result = self.gpu_predictor.run([data_tensor])
            else:
                result = self.cpu_predictor.run([data_tensor])
            for res in result[0].as_ndarray():
                res_dict = {}
                pred_label = np.argsort(res)[::-1][:top_k]
                for k in pred_label:
                    class_name = self.label_names[int(k)].split(',')[0]
                    max_prob = res[k]
                    res_dict[class_name] = max_prob
                res_list.append(res_dict)
        return res_list

    def add_module_config_arg(self):
        """
        Add the command config options
        """
        self.arg_config_group.add_argument(
            '--use_gpu', type=ast.literal_eval, default=False, help="whether use GPU or not")

        self.arg_config_group.add_argument('--batch_size', type=int, default=1, help="batch size for prediction")

    def add_module_input_arg(self):
        """
        Add the command input options
        """
        self.arg_input_group.add_argument('--input_path', type=str, default=None, help="input data")

        self.arg_input_group.add_argument('--input_file', type=str, default=None, help="file contain input data")

    def check_input_data(self, args):
        input_data = []
        if args.input_path:
            input_data = [args.input_path]
        elif args.input_file:
            if not os.path.exists(args.input_file):
                raise RuntimeError(f"File {args.input_file} is not exist.")
            else:
                input_data = txt_parser.parse(args.input_file, use_strip=True)
        return input_data

    @runnable
    def run_cmd(self, argvs):
        self.parser = argparse.ArgumentParser(
            description=f"Run the {self.name}",
            prog=f"hub run {self.name}",
            usage='%(prog)s',
            add_help=True,
        )
        self.arg_input_group = self.parser.add_argument_group(title="Input options", description="Input data. Required")
        self.arg_config_group = self.parser.add_argument_group(
            title="Config options", description="Run configuration for controlling module behavior, not required.")
        self.add_module_config_arg()
        self.add_module_input_arg()
        args = self.parser.parse_args(argvs)
        input_data = self.check_input_data(args)
        if len(input_data) == 0:
            self.parser.print_help()
            exit(1)
        else:
            for image_path in input_data:
                if not os.path.exists(image_path):
                    raise RuntimeError("File %s or %s is not exist." % image_path)
        return self.classification(paths=input_data, use_gpu=args.use_gpu, batch_size=args.batch_size)
