from __future__ import absolute_import
from flask import Flask, request
import json
import os
import requests
import numpy as np
from threading import Thread, Event
import queue
import tensorflow as tf
import os
import config

app = Flask(__name__)


# TRAINER and FEEDER CLASSES #
class Trainer(Thread):

    def __init__(self, trainable_queue, unlabelled_queue, test_queue, daemon=True):
        Thread.__init__(self, daemon=daemon)
        self.train_queue = train_queue
        self.unlabelled_queue = unlabelled_queue
        self.test_queue = test_queue
        self.started = False
        self.eval_and_query_countdown = 0

    def init(self, model):
        self.model = model
        self.started = True
        self.first_iter = True

    def SumEntropy(self, pred):
        def Entropy(prob):
            return -prob*np.log(prob)
        Entropy_vect = np.vectorize(Entropy)
        return np.sum(Entropy_vect(pred), dtype=np.float64)

    def sort_func(self, list_of_score_dicts):
        '''E/E strat, currently only decreasingly sorting by score'''
        return sorted(list_of_score_dicts, key = lambda i: (i["score"]), reverse=True)

    def send_sorted_data(self, data):
        r = requests.post(config.LABELER_IP+"/retrieve_query", data=json.dumps(data))

    def make_query(self, data,
                uncertainty_measure=config.uncertainty_measure, EEstrat=config.ee_strat):
        dict_keys = ["filename", "score"]
        list_of_score_dicts = []
        dataset = data[0]
        filenames = data[1]
        preds = self.model.predict(dataset)
        for i,p in enumerate(preds):
            score_dict = dict.fromkeys(dict_keys)
            score_dict["score"] = uncertainty_measure(p)
            score_dict["filename"] = filenames[i]
            list_of_score_dicts.append(score_dict)
        return EEstrat(list_of_score_dicts)

    def update_train_set(self, previous_train_set=None):
        if self.train_queue.qsize()>0 or previous_train_set==None:
            temp = []
            k = 0
            while self.train_queue.qsize() > 0 or k==0:
                k+=1
                self.eval_and_query_countdown+=1
                temp.append(self.train_queue.get())
            train_set = temp[0]
            if len(temp)>1:
                for k in range(len(temp)-1):
                    train_set.concatenate(temp[k+1])
            print(f"We got fed new training data! Number of requests before new evaluation and query : {config.EVAL_AND_QUERY_EVERY-self.eval_and_query_countdown}")
            return train_set
        else:
            return previous_train_set

    def update_unlabelled_data(self):
        k = 0
        while self.unlabelled_queue.qsize() > 0 or k==0:
            k+=1
            unlabelled_data = self.unlabelled_queue.get()
        return unlabelled_data

    def run(self):
        print("Waiting for the test set")
        test_set = self.test_queue.get()
        print("Test set acquired, starting training")
        while not stopTrainer.is_set():
            if self.first_iter:
                self.first_iter = False
                train_set = self.update_train_set()
            else:
                train_set.concatenate(self.update_train_set(train_set))
            self.model.fit(train_set, epochs=config.NUM_EPOCHS_PER_LOOP, verbose=config.TRAINING_VERBOSITY)
            if self.unlabelled_queue.qsize() > 0:
                unlabelled_data = self.update_unlabelled_data()
                if self.eval_and_query_countdown >= config.EVAL_AND_QUERY_EVERY:
                    print("Model evaluation")
                    self.eval_and_query_countdown = 0
                    evaluation = self.model.evaluate(test_set)
                    print("Evaluation result:")
                    for e,n in zip(evaluation, self.model.metrics_names):
                        print(f"{n} is {e}")
                        tresh = config.EARLY_STOPPING_METRICS_TRESHOLDS.get(n)
                        if tresh:
                            if e>=tresh:
                                print(f"Treshold ({tresh}) reached for {n}")
                                stopTrainer.set()
                                ## Add stopped request to labeler
                    if not stopTrainer.is_set():
                        print("Starting predictions")
                        sorted_unlabelled_data = self.make_query(unlabelled_data)  
                        print("Sending query")
                        self.send_sorted_data(sorted_unlabelled_data) 
        print("Stopping")
        self.model.save(config.SAVED_MODEL_DIR)
        print("Model saved, you can safely shut down the server")

# UTILS #
def decode_img(file_path):
    img = tf.io.read_file(file_path)
    img = tf.image.decode_jpeg(img, channels=3) #TODO: Support for grayscale image
    img = tf.image.convert_image_dtype(img, tf.float32)
    img = tf.image.resize(img, config.INPUT_SHAPE)
    return tf.keras.applications.mobilenet_v2.preprocess_input(img)

def save_test_data(data):
    if not os.path.isdir(config.ANNOTATIONS_SAVE_PATH):
        os.mkdir(config.ANNOTATIONS_SAVE_PATH)
    path = os.path.join(config.ANNOTATIONS_SAVE_PATH, "annotations.json")
    if os.path.isfile(path):
        return
    with open(path, "w") as f:
        json.dump(data, f)


def save_training_annotations(data):
    if not os.path.isdir(config.ANNOTATIONS_SAVE_PATH):
        os.mkdir(config.ANNOTATIONS_SAVE_PATH)
    path = os.path.join(config.ANNOTATIONS_SAVE_PATH, "annotations.json")
    if os.path.isfile(path):
        with open(path, 'r') as f:
            data_json = json.load(f)
        if "labelled_data" in data_json:
            data_json["labelled_data"][0].extend(data["labelled_data"][0])
            data_json["labelled_data"][1].extend(data["labelled_data"][1])
        else:
            data_json["labelled_data"] = data["labelled_data"]
        data_json["unlabelled"] = data["unlabelled"]
    else:
        data_json = data
    with open(path, "w") as f:
        json.dump(data_json, f)


def dataset_set_creation(data, num_classes):
    dataset = tf.data.Dataset.from_tensor_slices(tuple(data))     
    def pre_pro_training(file_path, label): 
        img = decode_img(file_path)
        label = tf.one_hot(label, depth=num_classes)
        return (img, label)
    return dataset.map(pre_pro_training).shuffle(config.SHUFFLE_BUFFER_SIZE).batch(config.BATCH_SIZE)

def unlabelled_set_creation(data):
    def pre_pro_unlabelled(file_path):
        img = decode_img(file_path)
        return img
    dataset = tf.data.Dataset.from_tensor_slices(data)
    return [dataset.map(pre_pro_unlabelled).batch(config.BATCH_SIZE), data]

def feed_test_data(data, labels_list):
    num_classes = len(labels_list)
    test_set = dataset_set_creation(data, num_classes=num_classes)
    test_queue.put(test_set)


def feed_training_data(data, labels_list):
    num_classes = len(labels_list)
    train_set = dataset_set_creation(data, num_classes=num_classes)
    train_queue.put(train_set)

def feed_query_data(data):
    unlabelled_data = unlabelled_set_creation(data) 
    unlabelled_queue.put(unlabelled_data)

## Server routes ##


@app.route("/stop_training", methods=["POST"])
def stop_training():
    if trainer.started:
        stopTrainer.set()
        trainer.join()
    return "Training Stopped"

@app.route("/init_training", methods=["POST"]) ## No need for async since need to wait for it
def send_init_sig():
    '''Init the worker thread'''
    data = json.loads(request.data)
    labels_list = data["labels_list"]
    num_classes = len(labels_list)
    if not trainer.started:
        model = config.model_fn(num_classes)
        trainer.init(model)
        trainer.start()
    return "Trainer initialized"

@app.route('/train', methods=['POST'])
def retrieve_data():
    """Retrieve the images to feed to the trainer  Class

        reqs = {
            "labelled_data": [impaths, labels]
            "labels_list": self explanatory
            "unlabelled": [impaths] 
            }
    """
    data = json.loads(request.data)
    save_training_annotations(data)
    feed_training_data(data["labelled_data"], data["labels_list"])
    feed_query_data(data["unlabelled"])
    return ""

@app.route("/test_data", methods=["POST"])
def test_data():
    '''Retrieve the test data and send them to the trainer thread
        data = {"test_data":, "labels_list":}
    '''
    data = json.loads(request.data)
    save_test_data(data)
    feed_test_data(data["test_data"], data["labels_list"])
    return ""

## Queue, events and thread def ##

train_queue = queue.Queue()
test_queue = queue.Queue()
unlabelled_queue = queue.Queue()
trainer = Trainer(train_queue, unlabelled_queue, test_queue)
stopTrainer = Event()

if __name__ == '__main__':
    app.run(host="localhost", port=3333, debug=True, use_reloader=False)
    