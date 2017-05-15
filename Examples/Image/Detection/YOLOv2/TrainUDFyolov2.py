# ==============================================================================
# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================


from cntk.ops.functions import UserFunction
from cntk.ops import *
import numpy as np
import math
from PARAMETERS import *

class TrainFunction(UserFunction):

    ####### Constructor #######
    def __init__(self,
                 arg1, gtbs,
                 grid_size_hor=(par_image_width/par_downsample),
                 grid_size_ver=(par_image_height / par_downsample),
                 num_anchorboxes=par_num_anchorboxes,
                 anchorbox_scales=par_anchorbox_scales,
                 num_gtbs_per_img=par_max_gtbs,
                 lambda_coord =5.0,
                 lambda_no_obj = 0.5,
                 name="TrainFunction"):
        super(TrainFunction, self).__init__([arg1, gtbs], name=name)

        self.grid_size_hor = int(grid_size_hor)
        self.grid_size_ver = int(grid_size_ver)
        self.num_anchorboxes = int(num_anchorboxes)
        assert lambda_no_obj <= 1, "lambda_no_obj must be smaller or equal 1"
        self.lambda_no_obj = lambda_no_obj
        self.lambda_coord = lambda_coord
        self.anchorbox_scales  = anchorbox_scales
        self.num_gtbs_per_img = int(num_gtbs_per_img)

    ######### @Overrides #########
    # @Override
    def forward(self, arguments, outputs=None, keep_for_backward=None, device=None, as_numpy=False):
        targets, scales = self.create_outputs(arguments[0], arguments[1])

        outputs[self.outputs[0]] = np.ascontiguousarray(targets, np.float32)
        outputs[self.outputs[1]] = np.ascontiguousarray(scales, np.float32)

        if False:
            self.check_values(arguments[0], outputs[self.outputs[0]], outputs[self.outputs[1]])

        return None

    # @Override
    def backward(self, state, root_gradients, variables):
        return None

    # @Override
    def infer_outputs(self):
        return [output_variable(self.inputs[0].shape, self.inputs[0].dtype, self.inputs[0].dynamic_axes, needs_gradient=False),
                output_variable(self.inputs[0].shape, self.inputs[0].dtype, self.inputs[0].dynamic_axes, needs_gradient=False)]

    # @Override
    def serialize(self):
        internal_state = {}
        internal_state['grid_size_hor'] = self.grid_size_hor
        internal_state['grid_size_ver'] = self.grid_size_ver
        internal_state['num_anchorboxes'] = self.num_anchorboxes
        internal_state['anchorbox_scales'] = self.anchorbox_scales
        internal_state['lambda_no_obj'] = self.lambda_no_obj
        internal_state['lambda_coord'] = self.lambda_coord
        internal_state['num_gtbs_per_mb'] = self.num_gtbs_per_img
        return internal_state

    # @Override
    @staticmethod
    def deserialize(inputs, name, state):
        # im_info = state['im_info']
        grid_size_hor = state['grid_size_hor']
        grid_size_ver = state['grid_size_ver']
        num_anchorboxes = state['num_anchorboxes']
        anchorbox_scales = state['anchorbox_scales']
        lambda_no_obj = state['lambda_no_obj']
        lambda_coord = state['lambda_coord']
        num_gtbs_per_mb = state['num_gtbs_per_mb']
        return TrainFunction(inputs[0], inputs[1],
                             grid_size_hor=grid_size_hor, grid_size_ver=grid_size_ver,
                             num_anchorboxes=num_anchorboxes, anchorbox_scales=anchorbox_scales,
                             num_gtbs_per_img=num_gtbs_per_mb,
                             lambda_coord=lambda_coord, lambda_no_obj=lambda_no_obj,
                             name=name)

    ######## Setter ############

    def set_lambda_coord(self, value):
        self.lambda_coord = value

    def set_lambda_no_obj(self, value):
        assert value <= 1, "lambda_no_obj must be smaller or equal 1"
        self.lambda_no_obj = value

    ####### user functions ##########

    def create_xywho_outputs(self, eval_results, gtb_inputs):
        # get dimensions
        mb_size = eval_results.shape[0]
        num_vectors = eval_results.shape[1]
        assert num_vectors == self.grid_size_hor * self.grid_size_ver * self.num_anchorboxes, "number of vectors do not match"
        vector_length = eval_results.shape[2]
        assert vector_length > 5, "Vector is too short! Length must be >5 (x,y,w,h,obj,cls...)"
        num_cls = vector_length - 5
        # remove asserions when tested?

        list_scales = [[[0] * 4 + [self.lambda_no_obj]] * num_vectors] * mb_size
        # set default values first so not every position needs to be written manually
        array_goal = np.zeros((mb_size, num_vectors, 5), np.float32)
        array_scale = np.asarray(list_scales, np.float32)

        for sample_idx in range(mb_size):

            gtb_array = gtb_inputs[sample_idx].reshape((int(len(gtb_inputs[sample_idx]) / 5), 5))  # shape (50,5)
            # Here we are going completely for numpy and for loops and no cntk/parallised ops! TODO Switch to CNTK implementation
            for i in range(len(gtb_array)):
                if gtb_array[i][4] == 0: break  # gtb list is in the padding area! (cls cannot be 0!)

                ###### x,y,w,h,obj #######

                x = min(int(gtb_array[i][0] * self.grid_size_ver), self.grid_size_ver - 1)
                y = min(int(gtb_array[i][1] * self.grid_size_hor), self.grid_size_hor - 1)

                # Find ab with highest iou with gtb
                highest_iou_index = None
                highest_iou = 0
                for z in range(self.num_anchorboxes):
                    ab_iou = self.calc_iou((gtb_array[i][0], gtb_array[i][1], gtb_array[i][2], gtb_array[i][3]),
                                           ((x + 0.5) * 1.0 / self.grid_size_hor, (y + 0.5) * 1.0 / self.grid_size_ver,
                                            self.anchorbox_scales[z][0], self.anchorbox_scales[z][1]))

                    if ab_iou > highest_iou:
                        highest_iou = ab_iou
                        highest_iou_index = z

                # if that ab exists: set goal and scale on x,y,w,h,obj
                if (highest_iou_index is not None):
                    vector_index = y * self.grid_size_hor * self.num_anchorboxes + x * self.num_anchorboxes + highest_iou_index

                    predicted_bb = eval_results[sample_idx][vector_index][0:4]
                    actual_iou = self.calc_iou(gtb_array[i][0:4], predicted_bb)

                    # BUT set only if the gridcell is not already responsible for another gtb with higher iou
                    if actual_iou > array_goal[sample_idx][vector_index][4]:
                        array_goal[sample_idx][vector_index][0] = gtb_array[i][0]
                        array_goal[sample_idx][vector_index][1] = gtb_array[i][1]
                        array_goal[sample_idx][vector_index][2] = gtb_array[i][2]
                        array_goal[sample_idx][vector_index][3] = gtb_array[i][3]
                        array_goal[sample_idx][vector_index][4] = actual_iou

                        # set scale for responsible vector
                        for z in range(4):
                            array_scale[sample_idx][vector_index][z] = self.lambda_coord
                        array_scale[sample_idx][vector_index][4] = 1

                        # delete the default lamda_no_obj from the scales of the other vectors
                        for ab in range(self.num_anchorboxes):
                            index = y * self.grid_size_ver * self.num_anchorboxes + x * self.num_anchorboxes + ab
                            if index != vector_index:
                                array_scale[sample_idx][index][4] = 0 # set obj of non relevant values --> 0

        return array_goal, array_scale

    def create_cls_outputs(self, gtb_inputs):
        # get dimensions
        mb_size = gtb_inputs.shape[0]
        num_vectors = self.grid_size_hor * self.grid_size_ver * self.num_anchorboxes
        vector_length = 5+par_num_classes
        assert vector_length > 5, "Vector is too short! Length must be >5 (x,y,w,h,obj,cls...)"
        num_cls = vector_length - 5
        # remove asserions when tested?

        list_scales = [[[0] * num_cls] * num_vectors] * mb_size
        # set default values first so not every position needs to be written manually
        array_goal = np.zeros((mb_size, num_vectors, num_cls), np.float32)
        array_scale = np.asarray(list_scales, np.float32)

        if True:
            ####### np-version ###############
            gtb_inputs.shape = (gtb_inputs.shape[0], int(gtb_inputs.shape[1]/5), 5)
            gtb_xmins = np.floor(np.maximum( (gtb_inputs[:, :, 0] - (.5* gtb_inputs[:,:,2])) * self.grid_size_hor, 0)).astype(int)
            gtb_xmaxs = np.floor(np.minimum( (gtb_inputs[:, :, 0] + (.5 * gtb_inputs[:, :, 2])) * self.grid_size_hor, self.grid_size_hor-1)).astype(int)

            gtb_ymins = np.floor(np.maximum((gtb_inputs[:, :, 1] - (.5 * gtb_inputs[:, :, 3])) * self.grid_size_ver, 0)).astype(int)
            gtb_ymaxs = np.floor(np.minimum((gtb_inputs[:, :, 1] + (.5 * gtb_inputs[:, :, 3])) * self.grid_size_ver, self.grid_size_ver-1)).astype(int)
            cls_indexs = (gtb_inputs[:,:,4] - 1).astype(int)

            original_shape = array_goal.shape
            array_goal.shape=(mb_size, self.grid_size_ver, self.grid_size_hor, self.num_anchorboxes, num_cls)

            for sample_idx in range(mb_size):
                for i in range(gtb_inputs.shape[1]):
                    cls_index = cls_indexs[sample_idx][i]
                    if cls_index < 0: break #first padding box reached!
                    array_goal[ sample_idx,
                                gtb_ymins[sample_idx][i]:gtb_ymaxs[sample_idx][i] + 1,
                                gtb_xmins[sample_idx][i]:gtb_xmaxs[sample_idx][i] + 1,
                                :,
                                cls_indexs[sample_idx][i] ] += 1
            array_goal.shape = original_shape

            divisor_wzero = np.add.reduce(array_goal, axis=2)
            divisor = np.maximum(divisor_wzero, 1)

            divisor.shape +=(1,)
            divisor_wzero.shape +=(1,)

            array_goal /= divisor
            array_scale = np.concatenate([divisor_wzero/divisor]*num_cls, axis =2)

            return array_goal, array_scale
            ##############################
        else:
            ####### loop-version ########
            for sample_idx in range(mb_size):

                divisors = [0]*num_vectors
                gtb_array = gtb_inputs[sample_idx].reshape((int(len(gtb_inputs[sample_idx]) / 5), 5))  # shape (50,5)
                # Here we are going completely for numpy and for loops and no cntk/parallised ops! TODO Switch to CNTK implementation
                for i in range(len(gtb_array)):
                    if gtb_array[i][4] == 0: break  # gtb list is in the padding area! (cls cannot be 0!)

                    left_gc_index = int((gtb_array[i][0] - 0.5 * gtb_array[i][2]) * self.grid_size_hor)
                    left_gc_index = max(left_gc_index, 0)
                    right_gc_index = int((gtb_array[i][0] + 0.5 * gtb_array[i][2]) * self.grid_size_hor)
                    right_gc_index = min(right_gc_index, self.grid_size_hor - 1)

                    top_gc_index = int((gtb_array[i][1] - 0.5 * gtb_array[i][3]) * self.grid_size_ver)
                    top_gc_index = max(top_gc_index, 0)
                    bottom_gc_index = int((gtb_array[i][1] + 0.5 * gtb_array[i][3]) * self.grid_size_ver)
                    bottom_gc_index = min(bottom_gc_index, self.grid_size_ver - 1)

                    cls_index = int(gtb_array[i][4]-1)

                    for x in range(left_gc_index, right_gc_index + 1):
                        for y in range(top_gc_index, bottom_gc_index + 1):
                            curr_vector_offset = (y * self.grid_size_hor + x) * self.num_anchorboxes
                            for z in range(self.num_anchorboxes):
                                array_goal[sample_idx][curr_vector_offset + z][cls_index] += 1
                                divisors[curr_vector_offset + z] += 1

                    for z in range(len(divisors)):
                        if divisors[z] > 0:
                            for s in range(num_cls):
                                array_scale[sample_idx][z][s]=1
                                array_goal[sample_idx][z][s] /= divisors[z]

            return array_goal, array_scale

    def create_outputs(self, eval_results, gtb_inputs):

        top_array_goal, top_array_scale = self.create_xywho_outputs(eval_results, gtb_inputs)
        bottom_array_goal, bottom_array_scale = self.create_cls_outputs(gtb_inputs)

        goal = np.append(top_array_goal, bottom_array_goal, axis=2)
        scale = np.append(top_array_goal, bottom_array_goal, axis=2)

        return goal, scale

    @staticmethod
    def calc_iou(coords1, coords2): #coords are tuples (x,y,w,h)
        area1 = coords1[2] * coords1[3] # width * height
        area2 = coords2[2] * coords2[3]
        assert  area1 >= 0,"Area 1 must not be below 0"
        assert area2 >= 0, "Area 2 must not be below 0"

       # intersection
        intersection = 0
        xl1 = coords1[0] - coords1[2]/2
        xr1 = coords1[0] + coords1[2] / 2
        xl2 = coords2[0] - coords2[2] / 2
        xr2 = coords2[0] + coords2[2] / 2
        yt1 = coords1[1] - coords1[3] / 2
        yb1 = coords1[1] + coords1[3] / 2
        yt2 = coords2[1] - coords2[3] / 2
        yb2 = coords2[1] + coords2[3] / 2

        left, right, top, bottom = None, None, None, None
        #get left bound of iou
        if xl1 <= xl2 <= xr1 :
            left = xl2
        elif xl2 <= xl1 <= xr2:
            left = xl1

        if xl1 <= xr2 <= xr1:
            right = xr2
        elif xl2 <= xr1 <= xr2:
            right = xr1

        if yt1 <= yt2 <= yb1 :
            top = yt2
        elif yt2 <= yt1 <= yb2:
            top = yt1

        if yt1 <= yb2 <= yb1:
            bottom = yb2
        elif yt2 <= yb1 <= yb2:
            bottom = yb1

        if left is not None and right is not None and top is not None and bottom is not None:
            intersection = (right - left) * (bottom - top)
        else:
            intersection = 0
        assert intersection >= 0, "Intersection must be equal or greater 0"

        union = (area1 + area2 - intersection)
        return intersection / union

    ######## applies sqrt() to w & h of the volume; uses cntk ops ######### (YOLOv1 only?)

    @staticmethod
    def make_wh_sqrt3(network):
        xy = network[:,:,0:2]
        wh = network[:,:,2:4]
        rest=network[:,:,4:]

        sqrt_wh = sqrt(wh)
        spliced = splice(xy,sqrt_wh,rest,axis=2)

        return spliced

    @staticmethod
    def make_wh_sqrt(network):
        xy = network[:,0:2]
        wh = network[:,2:4]
        rest=network[:,4:]

        sqrt_wh = sqrt(wh)
        spliced = splice(xy,sqrt_wh,rest,axis=1)

        return spliced

    ######### Check values in numpy ##########

    def sqrt_np_wh(self, volume):
        xy = volume[:, 0:2]
        wh = volume[:, 2:4]
        rest = volume[:, 4:]

        sqrt_wh = np.sqrt(wh)
        ap1 = np.append(xy, sqrt_wh, axis=1)
        ap2 = np.append(ap1, rest, axis=1)
        return ap2  # np.append(xy, sqrt_wh, rest, axis=1)

    # this is a test-function for development and may be disabled during actual training
    def check_values(self, eval_values, target, scale):
        if (np.isnan(eval_values).any()):
            print("Model output contained nan!")
            print(eval_values.shape)
        elif np.greater(eval_values, 100).any():
            print("greater 100!")
            print(np.max(eval_values))

            # import ipdb
            # ipdb.set_trace()

        if (np.isnan(target).any()):
            print("error_volume contained nan!")

        if (np.equal(target[:, :, 4:5], 1).any()):
            print("objectness target == 1! NOT ALLOWED!")

        if ((np.greater(scale[:, :, 2:4], 0) & np.equal(target[:, :, 2:4], 0)).any()):
            # if(np.where(scale[ np.where(np.equal(target[:,:,2:4], 0))]>0)[0].any() ):
            print("Scale is > 0 where target == 0 --> NOT ALLOWED")

        if (np.isnan(scale).any()):
            print("scale_volume contained nan!")

        # err = TrainFunction2.sqrt_np_wh(self,eval_values) - TrainFunction2.sqrt_np_wh(self,error)  # substrac "goal" --> error
        err = eval_values - target
        sq_err = err * err
        sc_err = sq_err * scale  # apply scales (lambda_coord, lambda_no_obj, zeros on not learned params)
        mse = np.add.reduce(sc_err, axis=None)
        if (math.isnan(float(mse))):
            print("mse is nan!")
            import ipdb
            ipdb.set_trace()
            exit()
