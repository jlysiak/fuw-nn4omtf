# -*- coding: utf-8 -*-
"""
    Copyright (C) 2018 Jacek Łysiak
    MIT License

    Neural network based muon momentum classifier trainer and tester.
"""

import tensorflow as tf
import time
import os
import threading
import subprocess

from nn4omtf.dataset import OMTFDataset
from nn4omtf.network import OMTFNN
from nn4omtf.utils import init_uninitialized_variables, dict_to_object

from nn4omtf.network.runner_helpers import collect_statistics,\
        setup_metrics, setup_trainer
from nn4omtf.network.input_pipe import OMTFInputPipe
from nn4omtf.const import PIPE_EXTRA_DATA_NAMES, NN_HOLDERS_NAMES,\
        PHASE_NAME, CNAMES


class OMTFRunner:
    """OMTFRunner  is base class for neural nets training and testing.
    It's reading data from TFRecord OMTF dataset created upon
    OMTF simulation data. Input pipe is automaticaly established.
    Main goal is to prepare 'placeholder' and provide universal
    input and output interface for different net's architectures.

    Dataset can be created using OMTFDataset class provided in this package.
    """

    DEFAULT_PARAMS = {
        "valid_batch_size": 1000,
        "batch_size": 1000,
        "sess_prefix": "",
        "shuffle": False,
        "acc_ival": 1000,
        "epochs": 1,
        "steps": -1,
        "logdir": '.',
        "verbose": False,
        "learning_rate": 0.001,
        "shiftval": 600,
        "nullval": 0,
        "limit_valid_examples": None,
        "limit_test_examples": None,
        "debug": False,
        "log": 'none'
    }

    def __init__(self, dataset, network, **kw):
        """Create parametrized runner instance.
        Args:
            dataset: OMTFDataset object, data source
            network: OMTFNN object, working object
            **kw: additional keyword args,
                substitute defaults params if key matches
        """
        self.dataset = dataset
        self.network = network

        # Setup parameters
        params = OMTFRunner.DEFAULT_PARAMS
        for key, val in kw.items():
            if key in params:
                params[key] = val
        # Setup runner variables
        timestamp = time.strftime("%Y-%m-%d-%H-%M-%S")
        pref = params['sess_prefix']
        out_class_bins = self.network.pt_class
        var = {
            'timestamp': timestamp,
            'sess_name': "{}{}_{}".format(pref, self.network.name, timestamp),
            'in_type': self.network.in_type,
            'out_class_bins': out_class_bins,
            'out_len': len(out_class_bins) + 1
        }
        for k, v in var.items():
            params[k] = v
        self.params = params

    def _log_init(self):
        self.log_hnd = {
                PHASE_NAME.TRAIN: None,
                PHASE_NAME.VALID: None,
                PHASE_NAME.TEST: None
        }
        self.writers = self.log_hnd.copy()
        fname = os.path.join(self.params['logdir'], self.params['sess_name'])
        os.makedirs(fname)
        if self.params['log'] in ['txt', 'both']:
            if self.params['phase'] == PHASE_NAME.TRAIN:
                tname = os.path.join(fname,'train.txt')
                vname = os.path.join(fname,'valid.txt')
                tf = open(tname, 'w')
                vf = open(vname, 'w')
                self.log_hnd[PHASE_NAME.TRAIN] = tf
                self.log_hnd[PHASE_NAME.VALID] = vf
                tf.write(self._params_string())
                vf.write(self._params_string())
                
            else:
                tname = os.path.join(fname, 'test.txt')
                self.log_hnd[PHASE_NAME.TEST] = open(tname, 'w')

        if self.params['log'] in ['tb', 'both']:
            if self.params['phase'] == PHASE_NAME.TRAIN:
                tname = os.path.join(fname, PHASE_NAME.TRAIN)
                vname = os.path.join(fname, PHASE_NAME.VALID)
                self.writers[PHASE_NAME.TRAIN] = tf.summary.FileWriter(tname)
                self.writers[PHASE_NAME.VALID] = tf.summary.FileWriter(vname)
            else:
                tname = os.path.join(fname, PHASE_NAME.TEST)
                self.writers[PHASE_NAME.TEST] = tf.summary.FileWriter(tname)

    
    def log(self, phase, step, data):
        f = self.log_hnd[phase]
        if f is None:
            return
        timestamp = time.strftime("%Y-%m-%d-%H-%M-%S")
        f.write(timestamp + " @ " +str(step) + ": ")
        for n, d in data:
            f.write(n + "=" + str(d) + ", ")
        f.write('\n')
        f.flush()


    def log_summary(self, phase, step, summs):
        writer = self.writers[phase]
        if writer is None:
            return
        for summ in summs:
            writer.add_summary(summ, step)

    def _log_deinit(self):
        for k, v in self.log_hnd.items():
            if v is not None:
                v.close()


    def _get_verbose_printer(self, lvl=1):
        """Get verbose printer.
        Returns:
            Lambda which prints out its argument if verbose flag is set
        """
        vp = lambda s: print("OMTFRunner: " + s) if self.params['verbose'] >= lvl else None
        return vp


    def _update_params(self, params_dict):
        """Update runner parameters
        Args:
            params_dict: dict with new parameters
        Note that some values (like session name) won't be updated.
        """
        for k, v in params_dict.items():
            self.params[k] = v


    def _start_clock(self):
        """Start runner timers.
        Returns:
            dict with start timestamp
        """
        self.start = time.time()
        self.last = self.start
        self.start_datetime = time.strftime("%Y-%m-%d-%H-%M-%S")
        return {'start_datetime': self.start_datetime}
    

    def _next_tick(self):
        """Update clock.
        Returns dict with useful falues which can be directly
        passed into string.format() method.
        Returns:
            dict of:
                - datetime string
                - elapsed time (since start)
                - last time (between ticks)
        """
        datetime = time.strftime("%Y-%m-%d-%H-%M-%S")
        now = time.time()
        last = now - self.last
        elapsed = now - self.start
        self.last = now
        res = {
            'start_datetime': self.start_datetime,
            'datetime': datetime,
            'elapsed': elapsed,
            'last': last
        }
        return res


    def show_params(self):
        print(self._params_string())

    def _params_string(self):
        r = "==== OMTFRunner configuration\n"
        for k, v in self.params.items():
            r += "> {:.<20}:{}\n".format(k, v)
        r += "=============================\n"
        return r


    def train(self, **kw):
        """Run model training.
        Training logs are saved on disk if `logs` flag is set.
        Short summaries are always appended to OMTFNN object.
        
        Args:
            **kw: additional args which can update previously set params
        """
        kw['phase'] = PHASE_NAME.TRAIN
        self._update_params(kw)
        self._log_init()
        opt = dict_to_object(self.params)
        # opt.sess_name += "/train"
        vp = self._get_verbose_printer(lvl=1)
        vvp = self._get_verbose_printer(lvl=2)
        vvvp = self._get_verbose_printer(lvl=3)

        tf.reset_default_graph()
        vp("Preparing training session: %s" % opt.sess_name)
        vp("Creating input pipes...")
        with tf.name_scope("input_pipes"):
            # Input pipes configuration
            train_pipe = OMTFInputPipe(
                    dataset=self.dataset,
                    name=PHASE_NAME.TRAIN,
                    hits_type=opt.in_type,
                    out_class_bins=opt.out_class_bins,
                    batch_size=opt.batch_size,
                    shuffle=opt.shuffle,
                    reps=opt.epochs,
                    remap_data=(opt.nullval, opt.shiftval),
                    detect_no_signal=True
                    )
            valid_pipe = OMTFInputPipe(
                    dataset=self.dataset,
                    name=PHASE_NAME.VALID,
                    hits_type=opt.in_type,
                    batch_size=opt.valid_batch_size,
                    out_class_bins=opt.out_class_bins,
                    remap_data=(opt.nullval, opt.shiftval),
                    detect_no_signal=True,
                    limit_examples=opt.limit_valid_examples)
        self.show_params()

        with tf.Session() as sess:
            # Restore model and get I/O tensors
            _, tsd = self.network.restore(sess=sess, sess_name=opt.sess_name)

            # Get collection of initialized variables
            init = sess.graph.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
            vp("Loaded model: %s" % self.network.name)

            # ==== NETWORK PLACEHOLDERS
            pt_labels = tf.placeholder(tf.int8, shape=[None, opt.out_len],
                                    name="pt_labels")
            sgn_labels = tf.placeholder(tf.int8, shape=[None, 3],
                                    name="sgn_labels")
            # ==== SETUP TRAINER NODES
            logits_list = [
                    ("pt", tsd[OMTFNN.CONST.OUT_PT_NAME], pt_labels),
                    ("sgn", tsd[OMTFNN.CONST.OUT_SGN_NAME], sgn_labels)
            ]
            # Get update operation tensors for batch normalization
            # and make statistics updating during training
            # More info? See this:
            # https://www.tensorflow.org/api_docs/python/tf/contrib/layers/batch_norm
            update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
            with tf.control_dependencies(update_ops):
                train_names, train_ops, train_summ_ops, train_vals = setup_trainer(
                        train_list=logits_list,
                        learning_rate=opt.learning_rate)

            ops, metrics_init = setup_metrics(logits_list=logits_list)
            metrics_out = [out for _, out, _, _, _ in ops if out is not None]
            metrics_ops = [op for _, _, op, _, _ in ops]
            metrics_ups = [up for _, _, _, up, _ in ops]
            metrics_summ = [s for _, _, _, _, s in ops if s is not None]
            names = [x[0] for x in ops]
            cnt_op = metrics_ops[-1]

            holders = [
                tsd[OMTFNN.CONST.IN_HITS_NAME],
                sgn_labels,
                pt_labels
            ]
            hdict = dict([(k, v) for k, v in zip(NN_HOLDERS_NAMES, holders)])
            # summary_op = tf.summary.merge_all()

            # At after all, initialize new nodes
            init_uninitialized_variables(sess, initialized=init)
            train_pipe.initialize(sess)
            
            i = 1            
            stamp = self._start_clock()
            vp("{start_datetime} - training started".format(**stamp))
           
            try:
                while i <= opt.steps or opt.steps < 0:
                    # ======= TRAINING SECTION
                    # Fetch next batch of input data and its labels
                    # Ignore extra data during trainings 
                    ddict, edict = train_pipe.fetch()
                    if ddict is None:
                        vp("Train dataset is empty!")
                        break
                    # Prepare training feed dict
                    train_feed_dict = dict([(hdict[k], ddict[k]) for k in NN_HOLDERS_NAMES])
                    train_feed_dict[tsd[OMTFNN.CONST.IN_PHASE_NAME]] = True
                    if opt.debug:
                        _, train_summ, train_ent, train_outs = sess.run(
                                [train_ops, train_summ_ops, train_vals, metrics_out], 
                                feed_dict=train_feed_dict)
                        edict['PT_K_OUT'] = train_outs[0]
                        edict['SGN_K_OUT'] = train_outs[1]
                        if self.show_dbg(ddict, edict):
                            vp("Exiting...")
                            break
                    else:
                        # Do mini-batch iteration
                        _, train_summ, train_ent = sess.run(
                                [train_ops, train_summ_ops, train_vals], 
                                feed_dict=train_feed_dict)
# Logging that much is rather bad idea??
# Save CE suring validation
#                    self.log_summary(PHASE_NAME.TRAIN, i, train_summ) 
#                    self.log(PHASE_NAME.TRAIN, i, zip(['cross-pt', 'cross-sgn'], train_ent))
                    vvp("Training step: {step}\nCross entropy PT: {pt}\nCross entropy SGN: {sgn}".format(
                                step=i,
                                pt=train_ent[0],
                                sgn=train_ent[1]))
                    # ======= VALIDATION SECTION
                    if i % opt.acc_ival == 0:
                        vp("Validation @ step {step}".format(step=i))
                        sess.run(metrics_init)
                        valid_pipe.initialize(sess)
                        ex_cnt = 0
                        while True:
                            vvp("Examples processed: %d" % ex_cnt)
                            vdict, edict = valid_pipe.fetch()
                            if vdict is None:
                                break
                            # Prepare training feed dict
                            valid_feed_dict = dict([(hdict[k], vdict[k]) for k in NN_HOLDERS_NAMES])
                            valid_feed_dict[tsd[OMTFNN.CONST.IN_PHASE_NAME]] = False
                            sess.run(metrics_ups, feed_dict=valid_feed_dict)
                            ex_cnt = sess.run(cnt_op)
                            if opt.limit_valid_examples is not None:
                                if opt.limit_valid_examples <= ex_cnt:
                                    break
                        accs, summs = sess.run([metrics_ops, metrics_summ])
                        # Save also data from last batch before validation
                        # Just to have any estimation
                        self.log_summary(PHASE_NAME.VALID, i, summs + train_summ)
                        names += ['cross-pt', 'cross-sgn']
                        accs += train_ent
                        self.log(PHASE_NAME.VALID, i, zip(names, accs))
                        for x, y in zip(names, accs):
                            print(x, y)

                    self._next_tick()
                    i += 1

            except KeyboardInterrupt:
                vp("Training stopped by user!")
                
            # End of main while loop, training finished
            stamp = self._next_tick()
            vp("{datetime} - training finished!".format(**stamp))
            vp("Training took: {elapsed:.1f} sec,".format(**stamp))
            vp("Steps in total: %d" % i)

            # Save network state after training
            self.network.finish()
            self.network.save()
            vp("Model saved!")
            self._log_deinit()


    def test(self, **kw):
        """Test loaded model on test dataset.
        Args:
            **kw: additional args which can update previously set params
        Returns:
            Short dict-summary from whole run.
        """
        self._update_params(kw)
        opt = dict_to_object(self.params)
        opt.sess_name += "/test"
        vp = self._get_verbose_printer()

        tf.reset_default_graph()
        vp("Preparing test session: %s" % opt.sess_name)
        vp("Creating input pipe...")
        with tf.name_scope("input_pipes"):
            # Input pipes configuration
            test_pipe = OMTFInputPipe(
                    dataset=self.dataset,
                    name='test',
                    hits_type=opt.in_type,
                    out_class_bins=opt.out_class_bins,
                    batch_size=opt.valid_batch_size)
        self.show_params()

        with tf.Session() as sess:
            _, net_in, net_pt_out, net_sgn_out = self.network.restore(
                    sess=sess, sess_name=opt.sess_name)
            vp("Loaded model: %s" % self.network.name)
            pt_labels = tf.placeholder(tf.int8, shape=[None, opt.out_len],
                                    name="pt_labels")
            sgn_labels = tf.placeholder(tf.int8, shape=[None, 2],
                                    name="sgn_labels")
            net_pholders = [
                net_in,
                pt_labels,
                sgn_labels
            ]
            pt_acc_op, sgn_acc_op, pt_class_op, sgn_class_op = setup_accuracy(
                    net_pt_out=net_pt_out,
                    net_sgn_out=net_sgn_out,
                    pt_labels=pt_labels,
                    sgn_labels=sgn_labels)
             
            summary_op = tf.summary.merge_all()
            
            stamp = self._start_clock()
            vp("{start_datetime} - test started!".format(**stamp))

            summaries, acc_d, nn_stats = collect_statistics(
                    sess=sess,
                    sess_name=opt.sess_name,
                    pipe=test_pipe,            # test using valid set
                    net_pholders=net_pholders,  # net placeholders
                    net_pt_out=net_pt_out,      # pt logits out
                    net_sgn_out=net_sgn_out,    # sgn logits out
                    net_pt_class=pt_class_op,   # pt class out
                    net_sgn_class=sgn_class_op, # sgn class out
                    pt_acc=pt_acc_op, 
                    sgn_acc=sgn_acc_op,
                    summary_op=summary_op)      # summary operator
            nn_stats.set_bins(opt.out_class_bins)

            self.network.add_log(acc_d, 1, opt.sess_name)
            self.network.add_statistics(nn_stats)
            print(nn_stats)

            stamp = self._next_tick()

            vp("{datetime} - test finished!".format(**stamp))
            vp("Test run took: {last:.1f} sec.".format(**stamp))
            vp("Accuracy:\n\tpt: {pt:f}\n\tsgn: {sgn:f}\n".format(
                **acc_d))

        res = {
            'sess_name': opt.sess_name,
            'accuracy': acc_d,
            'model': self.network.name
        }
        return res


    def test_many_models(dataset, models_list, **kw):
        """Test all provided models on test dataset.
        Args:
            dataset: dataset to test on
            models_list: list of OMTFNN objects
            **kw: runner params
        Returns:
            list of #OMTFRunner.test results
        """
        test_results = []
        for model in models_list:
            runner = OMTFRunner(dataset, model, **kw)
            res = runner.test()
            test_results.append(res)
        return test_results

    def show_dbg(self, ddict, edict):
        """Show step-by-step debugger screen"""
        print("=================== DEBUGER")
        dlen = len(ddict['HITS'])
        for i in range(dlen):
            print("======= HITS")
            print(ddict['HITS'][i])
            print("======= LABELS")
            print("== PT")
            print(ddict['PT_LABEL'][i])
            print("== SIGN")
            print(ddict['SIGN_LABEL'][i])
            cols = list(edict.keys())
            cols.pop() # remove vectors
            cols.pop()
            hfmt = ["{%s:^15}" % e for e in cols]
            hfmt = " | ".join(hfmt) + "\n"
            dfmt = ["{%s:>15.3}" % e for e in cols]
            dfmt = " | ".join(dfmt) + "\n"
            s = hfmt.format(**dict([(k, k) for k in cols]))
            val_d = dict([(k, float(edict[k][i])) for k in cols])
            s += dfmt.format(**val_d)
            s += "\n"
            print(s)
            v = input("Next? [Y/n]")
            if v == "n" or v == "N":
                break
        v = input("Continue debugging? [Y/n]")
        if v == "n" or v == "N":
            return True
        return False
