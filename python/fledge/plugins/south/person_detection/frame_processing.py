import asyncio
import copy
import uuid
import logging
import os
import time
import subprocess

import threading
from threading import Thread
from aiohttp import web, MultipartWriter


import cv2
import numpy as np

from fledge.common import logger
from fledge.plugins.common import utils
import async_ingest

from fledge.plugins.south.person_detection.videostream import VideoStream
from fledge.plugins.south.person_detection.inference import Inference
from fledge.plugins.south.person_detection.web_stream import WebStream


_LOGGER = logger.setup(__name__, level=logging.INFO)


class FrameProcessor(Thread):
    def __init__(self, handle):
        # if floating point model is used we need to subtract the mean and divide 
        # by standard deviation
        self.input_mean = handle['input_mean']
        self.input_std = handle['input_std']

        # the height of the detection window on which frames are to be displayed
        self.camera_height = handle['camera_height']

        # the width of the detection window on which frames are to be displayed
        self.camera_width = handle['camera_width']

        model = handle['model_file']['value']
        labels = handle['labels_file']['value']
        self.asset_name = handle['self.asset_name']['value']
        enable_tpu = handle['enable_edge_tpu']['value']
        self.min_conf_threshold = float(handle['min_conf_threshold']['value'])

        model = os.path.join(os.path.dirname(__file__), "model", model)
        labels = os.path.join(os.path.dirname(__file__), "model", labels)

        with open(labels, 'r') as f:
            pairs = (l.strip().split(maxsplit=1) for l in f.readlines())
            labels = dict((int(k), v) for k, v in pairs)

        # instance of the self.inference class
        self.inference = Inference()
        _ = self.inference.get_interpreter(model, enable_tpu,
                                      labels, self.min_conf_threshold)

        source = int(handle['camera_id']['value'])

        if handle['self.enable_window']['value'] == 'true':
            self.enable_window = True
        else:
            self.enable_window = False

        # Initialize the stream object and start the thread that keeps on reading frames
        # This thread is independent of the Camera Processing Thread
        self.videostream = VideoStream(resolution=(self.camera_width, self.camera_height), source=source).start()
        # For using the videostream with threading use the following :
        # videostream = VideoStream(resolution=(self.camera_width, self.camera_height),
        # source=source, enable_thread=True).start()
        self.shutdown_in_progress = False

    def construct_readings(self, objs):
        """ Takes the detection results from the model and convert into readings suitable to insert into database.
             For Example
                Lets say a  single person is detected then there will be a single element in the array
                whose contents  will be
                               {'label': 'person',
                                'score': 64, # probability of prediction
                                'bounding_box': [xmin, ymin, xmax, ymax] # bounding box coordinates
                                }

                A reading will be constructed in the form given below :

                    reads = {
                            'person_' + '1' + '_' + 'label': 'person'
                            'person_' + '1' + '_' + 'score': 64
                            'person_' + '1' + '_' + 'x1': xmin
                            'person_' + '1' + '_' + 'y1': ymin
                            'person_' + '1' + '_' + 'x2': xmax
                            'person_' + '1' + '_' + 'y2': ymax
                            'count': 1
                            }

                Args:
                       x -> an array of detection results
                Returns: Readings to be inserted into database.
               Raises: None
           """

        reads = {}
        for r_index in range(len(objs)):
            reads['person_' + str(r_index + 1) + '_' + 'label'] = objs[r_index]['label']
            reads['person_' + str(r_index + 1) + '_' + 'score'] = objs[r_index]['score']
            reads['person_' + str(r_index + 1) + '_' + 'x1'] = objs[r_index]['bounding_box'][0]
            reads['person_' + str(r_index + 1) + '_' + 'y1'] = objs[r_index]['bounding_box'][1]
            reads['person_' + str(r_index + 1) + '_' + 'x2'] = objs[r_index]['bounding_box'][2]
            reads['person_' + str(r_index + 1) + '_' + 'y2'] = objs[r_index]['bounding_box'][3]

        reads['count'] = len(objs)

        return reads

    def wait_for_frame(self):
        """ Waits for frame to become available else sleeps for 200 milliseconds.
                Args:
                       x -> a videostream object
                Returns: None
               Raises: None
           """
        while True:
            if self.videostream.frame is not None:
                return
            else:
                time.sleep(0.2)

    def run(self):
        # these variables are used for calculation of frame per seconds (FPS)
        frame_rate_calc = 1
        freq = cv2.getTickFrequency()
        # The thread is allowed to capture a few frames. See FOGL-4132 for details
        self.wait_for_frame()

        while True:
            # Capture frame-by-frame
            t1 = cv2.getTickCount()

            # we need the height , width to resize the image for feeding into the model
            height_for_model = self.inference.height_for_model
            width_for_model = self.inference.width_for_model

            #  check if floating point model is used or not
            floating_model = self.inference.floating_model

            # The minimum confidence to threshold the detections obtained from model
            self.min_conf_threshold = self.inference.min_conf_threshold

            # The list of labels of the supported objects detected by the plugin
            labels = self.inference.labels

            # Taking the frame the stream  
            frame1 = self.videostream.read()
            frame = frame1.copy()

            # BGR to RGB 
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # resizing it to feed into model
            frame_resized = cv2.resize(frame_rgb, (width_for_model, height_for_model))
            # input_data will now become 4 dimensional 
            input_data = np.expand_dims(frame_resized, axis=0)
            # now it will have (batchsize, height, width, channel)

            # Normalize pixel values if using a floating model 
            # (i.e. if model is non-quantized)
            if floating_model:
                input_data = (np.float32(input_data) - self.input_mean) / self.input_std

            # Perform the actual detection by running the model with the image as input
            boxes, classes, scores = self.inference.perform_self.inference(input_data)

            # we could have got  number of objects 
            # but it does not work most of the times.

            # num = interpreter.get_tensor(output_details[3]['index'])[0]  #
            # Total number of detected objects (inaccurate and not needed)

            # The readings array to be inserted in the readings table
            objs = []

            # Loop over all detections and draw detection box
            #  if confidence is above minimum then  only 
            #  that detected object will  be considered

            # The index of person class is zero.
            for i in range(len(scores)):
                if (scores[i] > self.min_conf_threshold) and (int(classes[i] == 0)):
                    # Get bounding box coordinates and draw box
                    # Interpreter can return coordinates that are outside of image dimensions, 
                    # need to force them to be within image using max() and min()

                    ymin_model = round(boxes[i][0], 3)
                    xmin_model = round(boxes[i][1], 3)
                    ymax_model = round(boxes[i][2], 3)
                    xmax_model = round(boxes[i][3], 3)

                    # map the bounding boxes from model to the window 
                    ymin = int(max(1, (ymin_model * self.camera_width)))
                    xmin = int(max(1, (xmin_model * self.camera_height)))
                    ymax = int(min(self.camera_width, (ymax_model * self.camera_width)))
                    xmax = int(min(self.camera_height, (xmax_model * self.camera_height)))

                    # draw the rectangle on the frame
                    cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (10, 255, 0), 2)

                    # Contructing the label

                    # Look up object name from "labels" array using class index
                    object_name = labels[int(classes[i])]

                    # Example: 'person: 72%'
                    label = '%s: %d%%' % (object_name, int(scores[i] * 100))

                    # Get font size
                    labelSize, baseLine = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                                          0.7, 2)

                    # Make sure not to draw label too close to top of window
                    label_ymin = max(ymin, labelSize[1] + 10)

                    # Draw white box to put label text in
                    cv2.rectangle(frame, (xmin, label_ymin - labelSize[1] - 10),
                                  (xmin + labelSize[0], label_ymin + baseLine - 10),
                                  (255, 255, 255), cv2.FILLED)

                    # Draw the text label 
                    cv2.putText(frame, label, (xmin, label_ymin - 7),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)

                    # the readings to be inserted into the table
                    objs.append({'label': labels[classes[i]],
                                 'score': 100 * scores[i],
                                 'bounding_box': [xmin, ymin, xmax, ymax]
                                 })

            # Draw framerate in corner of frame
            cv2.putText(frame, 'FPS: {0:.2f}'.format(frame_rate_calc),
                        (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0)
                        , 2, cv2.LINE_AA)

            # All the results have been drawn on the frame, so it's time to display it.
            if self.shutdown_in_progress:
                self.videostream.stop()
                time.sleep(3)
                # cv2.destroyWindow(window_name)
                break
            else:
                # Calculate framerate
                t_end = cv2.getTickCount()
                time1 = (t_end - t1) / freq
                frame_rate_calc = 1 / time1

                reads = self.construct_readings(objs)
                data = {
                    'asset': self.asset_name,
                    'timestamp': utils.local_timestamp(),
                    'readings': reads
                }
                # async_ingest.ingest_callback(c_callback, c_ingest_ref, data)

                # show the frame on the window
                try:
                    if self.enable_window:
                        # cv2.imshow(window_name, frame)
                        pass
                    WebStream.FRAME = frame.copy()
                except Exception as e:
                    _LOGGER.info('exception  {}'.format(e))

                # wait for 1 milli second 
                cv2.waitKey(1)