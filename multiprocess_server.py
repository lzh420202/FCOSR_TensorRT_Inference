from multiprocessing import (Pipe, Lock, Queue, Process)
import numpy as np
import cv2
import os
import math
import time
from utils.nms import multiclass_poly_nms_rbbox, multiclass_poly_nms_rbbox_patches
from utils.visualize import draw_result
from utils.tools import (print_log, generate_split_box)
DEBUG = False


def preprocess_data_unit(pipe, queue, normalization):
    while True:
        data = pipe.recv()
        if data:
            src_image, boxes, meta, start_time = data
            for box in boxes:
                image = src_image[box[0]:box[1], box[2]:box[3], :].copy()
                h, w, _ = image.shape
                pad_h = meta['patch_size'] - h
                pad_w = meta['patch_size'] - w
                if pad_w > 0 or pad_h > 0:
                    image = cv2.copyMakeBorder(image, 0, pad_h, 0, pad_w, cv2.BORDER_CONSTANT, 0)
                if DEBUG:
                    new = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                    base_name = os.path.splitext(os.path.basename(meta["image_path"]))[0]
                    cv2.imwrite(os.path.join(f'/data/cache/{base_name}_{box[0]}_{box[2]}.jpg'), new)

                image = np.asarray(image, np.float32)
                if normalization['enable']:
                    cv2.subtract(image, normalization['mean'], image)
                    cv2.multiply(image, normalization['std'], image)
                image = np.expand_dims(np.transpose(image, [2, 0, 1]), 0).astype(np.float32)
                queue.put(dict(image=image,
                               image_path=meta['image_path'],
                               offset=(box[2], box[0]),
                               patch_num=meta['patch_num'],
                               start_time=start_time))
        else:
            queue.put(None)
            break


def preprocess_data_zmq(image_queue,
                        pipes,
                        result_log_recv,
                        lock: Lock,
                        split_cfg=dict(subsize=1024, gap=200)):
    log = dict()
    while True:
        lock.acquire()
        data = image_queue.get()
        if data:
            t = time.time()
            img = cv2.cvtColor(data['image'], cv2.COLOR_BGR2RGB)
            h, w, _ = img.shape
            boxes = generate_split_box((h, w), split_cfg['subsize'], split_cfg['gap'])
            per_list_num = math.ceil(len(boxes) / len(pipes))
            image_meta = dict(image_path=data['name'], patch_size=split_cfg['subsize'], gap=split_cfg['gap'], patch_num=len(boxes))

            for i, pipe in enumerate(pipes):
                per_boxes = boxes[i * per_list_num: (i + 1) * per_list_num]
                pipe.send((img, per_boxes, image_meta, t))
            log[data['name']] = image_meta
            log[data['name']]['shape'] = (w, h)
            log[data['name']]['det_num'] = result_log_recv.recv()
            log[data['name']]['time'] = time.time() - t
            meta = log[data['name']]
            print_log(data['name'], meta)
        else:
            break
    for pipe in pipes:
        pipe.send(None)


def preprocess_data(image_queue,
                    data_queue: Queue,
                    result_log_recv: Pipe,
                    num_processor,
                    lock: Lock,
                    normalization=dict(enable=True,
                                        mean=[123.675, 116.28, 103.53],
                                        std=[58.395, 57.12, 57.375]),
                    split_cfg=dict(subsize=1024,
                                    gap=200)):
    mean_ = np.array(normalization['mean'])
    mean = np.float64(mean_.reshape(1, -1))
    std = np.array(normalization['std'])
    stdinv = 1.0 / np.float64(std.reshape(1, -1))
    norm_cfg = dict(enable=normalization['enable'], mean=mean, std=stdinv)

    process = []
    pipe_sends = []
    pipe_recvs = []
    for i in range(num_processor):
        recv, send = Pipe(duplex=False)
        pipe_sends.append(send)
        pipe_recvs.append(recv)
    p = Process(target=preprocess_data_zmq, args=(image_queue, pipe_sends, result_log_recv, lock, split_cfg))
    process.append(p)
    for i in range(num_processor):
        p = Process(target=preprocess_data_unit, args=(pipe_recvs[i], data_queue, norm_cfg))
        process.append(p)

    return process


def postprocess_unit(input_queue: Queue, cache_queue: Queue, det_cfg):
    while True:
        input_data = input_queue.get()
        if input_data:
            boxes_, labels_ = multiclass_poly_nms_rbbox(input_data['box'],
                                                        input_data['score'],
                                                        det_cfg['score_threshold'],
                                                        det_cfg['nms_threshold'],
                                                        det_cfg['max_det_num'])
            boxes_[:, 0:8:2] += input_data['offset'][0]
            boxes_[:, 1:8:2] += input_data['offset'][1]

            cache_queue.put(dict(rboxes=boxes_,
                                 labels=labels_,
                                 image_path=input_data['image_path'],
                                 patch_num=input_data['patch_num'],
                                 class_num=input_data['score'].shape[1]))
        else:
            input_queue.put(None)
            cache_queue.put(None)
            break


def postprocess_collect(cache_queue: Queue, output_pipe: Pipe, log_pipe: Pipe, lock: Lock, det_cfg, num_processor):
    cache_box = []
    cache_label = []
    patch_count = 0
    image_path = ''
    count = 0
    while True:
        cache_data = cache_queue.get()
        if cache_data:
            if image_path == '':
                image_path = cache_data['image_path']
            assert image_path == cache_data['image_path']
            cache_box.append(cache_data['rboxes'])
            cache_label.append(cache_data['labels'])
            patch_count += 1

            if patch_count == cache_data['patch_num']:
                boxes_ = np.concatenate(cache_box, axis=0)
                labels_ = np.concatenate(cache_label, axis=0)
                boxes, labels = multiclass_poly_nms_rbbox_patches(boxes_,
                                                                  labels_,
                                                                  cache_data['class_num'],
                                                                  det_cfg['nms_threshold'])
                output_pipe.send(dict(rboxes=boxes, labels=labels, image_path=image_path))
                log_pipe.send(len(labels))
                cache_box = []
                cache_label = []
                patch_count = 0
                image_path = ''
                lock.release()
                continue
        else:
            count += 1
            if count == num_processor:
                output_pipe.send(None)
                break
            else:
                continue


def postprocess(num_processor,
                input_queue: Queue,
                output_pipe: Pipe,
                log_pipe: Pipe,
                cache_size: int,
                lock: Lock,
                det_cfg):
    process = []
    cache_queue = Queue(cache_size)
    for i in range(num_processor):
        p = Process(target=postprocess_unit, args=(input_queue, cache_queue, det_cfg))
        process.append(p)
    p = Process(target=postprocess_collect, args=(cache_queue, output_pipe, log_pipe, lock, det_cfg, num_processor))
    process.append(p)
    return process


def output_result(zmq_result_queue, ALL_LABEL, result_pipe):
    while True:
        result = result_pipe.recv()
        if result:
            # det_str = ''
            box = result['rboxes']
            label = result['labels']
            # image_path = result['image_path']

            objs = [dict(label=ALL_LABEL[label_], box=box[j, :-1].tolist(), confidence=box[j, -1]) for j, label_ in enumerate(label)]
            zmq_result_queue.put(dict(image=result['image_path'], objects=objs))
            # objs = []
            # for j, label_ in enumerate(label):
            #     name = ALL_LABEL[label_]
            #     objs.append(dict(label=name, box=box[j, :-1].tolist(), confidence=box[j, -1]))
        else:
            break