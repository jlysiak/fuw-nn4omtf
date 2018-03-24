# -*- coding: utf-8 -*-
"""
    Copyright (C) 2018 Jacek Łysiak
    MIT License

    OMTFInputPipe static helper methods
"""
import numpy as np
import time
import multiprocessing
import tensorflow as tf

from nn4omtf.dataset.const import NPZ_FIELDS, HITS_TYPE
from nn4omtf.network.input_pipe_const import \
    PIPE_MAPPING_TYPE, PIPE_EXTRA_DATA_NAMES


    

def _deserialize(x, hits_type, out_len, out_class_bins):
    """Deserialize hits and convert pt value into categories using
    with one-hot encoding.

    NOTICE: data returned from OMTF algorithm is a bit wired.
    In case of mismatch we get charge=-2 and pt = -999.0.
    If we want to compare OMTF with NN, digitizing of OMTF data is
    performed with additional bins. Resulting value is shifted to match
    NN ranges. There are some other problem with compating bins only
    but this case is the most frequent.
    Comparison of NN and OMTF can be done only by comparing bucket due to
    NN is simple clasifier.

    Args:
        hits_type: hits type const from `HITS_TYPE`
        out_len: output one-hot tensor length
        bucket_fn: float value classifier function
    
    Returns:
        data on single event which is 3-tuple containing:
            - selected hits array (HITS_REDUCED | HITS_FULL)
            - dict of labels for: 
                - production pt (1-hot encoded)
                - production charge sign (0 negative, 1 positive)
            - dict with extra data:
                - exact value of pt
                - used pt code
                - omtf recognized pt (1-hot)
                - omtf recognized sign 
                    (because this value may be equal -2... read bucket
                    function description)
    """

    prod_dim = NPZ_FIELDS.PROD_SHAPE
    omtf_dim = NPZ_FIELDS.OMTF_SHAPE

    hits = NPZ_FIELDS.HITS_REDUCED
    hits_dim = HITS_TYPE.REDUCED_SHAPE
    if hits_type == HITS_TYPE.FULL:
        hits = NPZ_FIELDS.HITS_FULL
        hits_dim = HITS_TYPE.FULL_SHAPE

    # Define features to read & deserialize from TFRecords dataset 
    features = {
        hits: tf.FixedLenFeature(hits_dim, tf.float32),
        NPZ_FIELDS.PROD: tf.FixedLenFeature(prod_dim, tf.float32),
        NPZ_FIELDS.OMTF: tf.FixedLenFeature(omtf_dim, tf.float32),
        NPZ_FIELDS.PT_CODE: tf.FixedLenFeature([1], tf.float32)
    }
    examples = tf.parse_single_example(x, features)

    hits_arr = examples[hits]
    prod_arr = examples[NPZ_FIELDS.PROD]
    omtf_arr = examples[NPZ_FIELDS.OMTF]
    pt_code = examples[NPZ_FIELDS.PT_CODE][0]

    # ==== Prepare customized no-param bucketizing functions
    prod_bucket_fn = lambda x: np.digitize(x=x, bins=out_class_bins)
    # Bucketize sign: 0 -> -, 1 -> +
    prod_sgn_bucket_fn = lambda x: np.digitize(x=x, bins=[0])

    omtf_bins = out_class_bins.copy()
    omtf_bins.insert(0, 0)
    omtf_bucket_fn = lambda x: (np.digitize(x=x, bins=omtf_bins) - 1)
    
    # Bucketizing function for OMTF charge sign parameter.
    # Given bins may seems to be very strange, but...
    # OMTF algo. in case of mismatch gives charge about -2.00...
    # Off by one to match production label.
    omtf_sgn_bucket_fn = lambda x: (np.digitize(x=x, bins=[-1.5, 0]) - 1)

    # ======= TRAINING DATA
    # Prepare production pt labels
    prod_pt_k = tf.py_func(prod_bucket_fn,
                   [prod_arr[NPZ_FIELDS.PROD_IDX_PT]],
                   tf.int64,
                   stateful=False,
                   name='digitize_prod_pt')
    # Encode pt
    prod_pt_label = tf.one_hot(prod_pt_k, out_len)

    # Encode signs
    prod_sign_k = tf.py_func(prod_sgn_bucket_fn,
                    [prod_arr[NPZ_FIELDS.PROD_IDX_SIGN]],
                    tf.int64,
                    stateful=False,
                    name='muon_sgn_code')
    prod_sign_label = tf.one_hot(prod_sign_k, 2)
    
    # ======= EXTRA OMTF DATA
    omtf_sign_k = tf.py_func(omtf_sgn_bucket_fn,
                    [omtf_arr[NPZ_FIELDS.OMTF_IDX_SIGN]],
                    tf.int64,
                    stateful=False,
                    name='omtf_sgn_code')

    # Encode omtf pt guessed values 
    omtf_pt_k = tf.py_func(omtf_bucket_fn,
                   [omtf_arr[NPZ_FIELDS.OMTF_IDX_PT]],
                   tf.int64,
                   stateful=False,
                   name='digitize_omtf_pt')

    # ======== EXTRA DATA DICT
    # See `PIPE_EXTRA_DATA_NAMES` for correct order of fields
    vals = [
        pt_code,
        prod_arr[NPZ_FIELDS.PROD_IDX_PT],
        prod_pt_k,
        prod_sign_k,
        omtf_arr[NPZ_FIELDS.OMTF_IDX_PT],
        omtf_pt_k,
        omtf_sign_k
    ]
    edata_dict = dict([(k, v) for k, v in zip(PIPE_EXTRA_DATA_NAMES, vals)])
    
    return examples[hits], prod_pt_label, prod_sign_label, edata_dict


def _new_tfrecord_dataset(filename, compression, parallel_calls, in_type,
                          out_len, out_class_bins):
    """Creates new TFRecordsDataset as a result of map function.
    It's interleaved in #setup_input_pipe method as a base of lambda.
    Interleave takes filename tensor from filenames dataset and pass
    it to this function. New TFRecordDataset is produced  by reading
    `block_length` examples from given file in cycles of defined
    earlier length. It's also the place for converting data from
    binary form to tensors.
    Args:
      filename: TFRecord file name
      compression: type of compression used to save given file
      parallel_calls: # of calls used to map
      in_type: input tensor type
      out_len: output one-hot tensor length
      bucket_fn: float value classifier function
    """
    # TFRecords can be compressed initially. Pass `ZLIB` or `GZIP`
    # if compressed or `None` otherwise.
    dataset = tf.data.TFRecordDataset(
        filenames=filename,
        compression_type=compression)
    # Create deserializing map function

    def map_fn(x): return _deserialize(x,
                                       hits_type=in_type,
                                       out_len=out_len,
                                       out_class_bins=out_class_bins)

    # Additional options to pass in dataset.map
    # - num_threads
    # - output_buffer_size
    return dataset.map(map_fn, num_parallel_calls=parallel_calls)


def setup_input_pipe(files_n, name, in_type, out_class_bins, compression_type,
                     batch_size=None, shuffle=False, reps=1, 
                     mapping_type=PIPE_MAPPING_TYPE.INTERLEAVE):
    """Create new input pipeline for given dataset.

    Args:
        files_n: # of files in dataset
        name: input pipe name, used in graph scope
        in_type: input tensor type, see: HITS_TYPE
        out_class_bins: list of bins edges to convert float into one-hot vector
        batch_size(optional,default=None): how many records should be read
            in one batch
        shuffle(optional,default=False): suffle examples in batch
        mapping_type(optional, default=interleave): selects which mapping
            type should be applied when producing examples dataset from
            filenames dataset
    Returns:
        2-tuple (filenames placeholder, dataset iterator)
    """
    # Number of cores for parallel calls
    cores_count = multiprocessing.cpu_count()

    with tf.name_scope(name):
        # File names as placeholder
        files_placeholder = tf.placeholder(tf.string, shape=[files_n])
        # Create dataset of input files
        files_dataset = tf.data.Dataset.from_tensor_slices(
            files_placeholder)
        # Shuffle file paths dataset
        files_dataset = files_dataset.shuffle(buffer_size=files_n)

        # Length of output tensor, numer of classes
        out_len = len(out_class_bins) + 1

        # Prepare customized map function: (filename) => Dataset.map(...)
        def map_fn(filename): return _new_tfrecord_dataset(
            filename,
            compression=compression_type,
            parallel_calls=cores_count,
            in_type=in_type,
            out_len=out_len,
            out_class_bins=out_class_bins)

        if mapping_type == PIPE_MAPPING_TYPE.INTERLEAVE:
            # Now create proper dataset with interleaved samples from each TFRecord
            # file. `interleave()` maps provided function which gets filename
            # and reads examples. They can be transformed. Results of map function
            # are interleaved in result dataset.
            # 
            # I recommend read the docs for more information:
            # https://www.tensorflow.org/api_docs/python/tf/data/Dataset#interleave
            dataset = files_dataset.interleave(
                map_func=map_fn,
                cycle_length=files_n,  # Length of cycle - go through all files
                block_length=1)       # One example from each input file in one cycle

        elif mapping_type == PIPE_MAPPING_TYPE.FLAT_MAP:
            dataset = files_dataset.flat_map(map_func=map_fn)

        else:
            raise ValueError("mapping_type param in not one of PIPE_MAPPING_TYPE constants.")

        # How many times whole dataset will be read.
        dataset = dataset.repeat(count=reps)

        if batch_size is not None:
            # How big one batch will be...
            dataset = dataset.batch(batch_size=batch_size)
            if shuffle:
                # Data will be read into the buffer and here we can suffle
                # it
                dataset = dataset.shuffle(
                    buffer_size=batch_size,
                    seed=int(time.time()))

        # Create initializable iterator. String placeholder will be used in sesson
        # and whole dataset will be created then with proper
        # set of input files, i.e. train, eval, test set of input files
        iterator = dataset.make_initializable_iterator()

    return files_placeholder, iterator
