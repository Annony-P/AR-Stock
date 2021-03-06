import argparse
import copy
import numpy as np
import os
# import psutil
import random
import tensorflow as tf
from time import time
import logging
try:
    from tensorflow.python.ops.nn_ops import leaky_relu
except ImportError:
    from tensorflow.python.framework import ops
    from tensorflow.python.ops import math_ops


    def leaky_relu(features, alpha=0.2, name=None):
        with ops.name_scope(name, "LeakyRelu", [features, alpha]):
            features = ops.convert_to_tensor(features, name="features")
            alpha = ops.convert_to_tensor(alpha, name="alpha")
            return math_ops.maximum(alpha * features, features)

from load_data import load_EOD_data, load_relation_data
from evaluator import evaluate



class ReRaLSTM:
    def __init__(self, data_path, market_name, tickers_fname, relation_name,
                 emb_fname, parameters, steps=1, epochs=50, batch_size=None, flat=False, in_pro=False, seed=123456789, geom=False,args=None):

        seed = seed
        random.seed(seed)
        np.random.seed(seed)
        tf.set_random_seed(seed)
        self.seed = seed
        self.data_path = data_path
        self.market_name = market_name
        self.tickers_fname = tickers_fname
        self.relation_name = relation_name
        self.gp = args.gp
        self.reg = args.self
        self.reg_b = args.self_b
        self.unify = args.unify
        self.two_way_b = args.two_way_b
        self.geom=geom
        # load data
        self.tickers = np.genfromtxt(os.path.join(data_path, '..', tickers_fname),
                                     dtype=str, delimiter='\t', skip_header=False)
        self.train_ratio = float(args.train_ratio)
        self.train_size = int(self.train_ratio*len(self.tickers))
        print('#tickers selected:', self.train_size)
        self.eod_data, self.mask_data, self.gt_data, self.price_data = \
            load_EOD_data(data_path, market_name, self.tickers, steps)

        # relation data
        rname_tail = {'sector_industry': '_industry_relation.npy',
                      'wikidata': '_wiki_relation.npy'}
        if geom:
            self.rel_encoding, self.rel_mask = load_relation_data(
                os.path.join(self.data_path, '..', 'relation', self.relation_name,
                            self.market_name + rname_tail[self.relation_name][:-4] + "_geom_{}.npy".format(args.thresh))
            )
        else:
            self.rel_encoding, self.rel_mask = load_relation_data(
                os.path.join(self.data_path, '..', 'relation', self.relation_name,
                            self.market_name + rname_tail[self.relation_name])
            )
        if self.reg=="part":
            self.part_label = np.load("../data/{}_part_{}.npy".format(self.market_name, args.gp))

        print('relation encoding shape:', self.rel_encoding.shape)
        print('relation mask shape:', self.rel_mask.shape)

        self.embedding = np.load(
            os.path.join(self.data_path, '..', 'pretrain', emb_fname))
        print('embedding shape:', self.embedding.shape)

        # for few training
        np.random.seed(int(args.train_ratio_seed))
        self.select_index = np.random.choice(len(self.tickers),size=self.train_size,replace=False)
        self.train_mask_data, self.train_gt_data, self.train_price_data = self.mask_data[self.select_index,:], self.gt_data[self.select_index,:], self.price_data[self.select_index,:]
        self.train_embedding = self.embedding[self.select_index,:,:]
        print("train embed gt:",self.train_embedding.shape,self.train_gt_data.shape)
        self.train_rel_encoding, self.train_rel_mask = self.rel_encoding[self.select_index,:,:][:,self.select_index,:], self.rel_mask[self.select_index,:][:,self.select_index]
        print("train rel_enc rel_mask:",self.rel_encoding.shape,self.train_rel_encoding.shape,self.train_rel_mask.shape)
        self.parameters = copy.copy(parameters)
        self.steps = steps
        self.epochs = epochs
        self.flat = flat
        self.inner_prod = in_pro
        if batch_size is None:
            self.batch_size = len(self.tickers)
        else:
            self.batch_size = batch_size

        self.valid_index = 756
        self.test_index = 1008
        self.trade_dates = self.mask_data.shape[1]
        self.fea_dim = 5

    def get_batch(self, offset=None):
        if offset is None:
            offset = random.randrange(0, self.valid_index)
        seq_len = self.parameters['seq']
        mask_batch = self.mask_data[:, offset: offset + seq_len + self.steps]
        mask_batch = np.min(mask_batch, axis=1)
        return self.embedding[:, offset, :], \
               np.expand_dims(mask_batch, axis=1), \
               np.expand_dims(
                   self.price_data[:, offset + seq_len - 1], axis=1
               ), \
               np.expand_dims(
                   self.gt_data[:, offset + seq_len + self.steps - 1], axis=1
               )

    def get_train_batch(self, offset=None):
        if offset is None:
            offset = random.randrange(0, self.valid_index)
        seq_len = self.parameters['seq']
        train_mask_batch = self.train_mask_data[:, offset: offset + seq_len + self.steps]
        train_mask_batch = np.min(train_mask_batch, axis=1)
        return self.train_embedding[:, offset, :], \
               np.expand_dims(train_mask_batch, axis=1), \
               np.expand_dims(
                   self.train_price_data[:, offset + seq_len - 1], axis=1
               ), \
               np.expand_dims(
                   self.train_gt_data[:, offset + seq_len + self.steps - 1], axis=1
               )


    def train(self):
        # if self.gpu == True:
        #     device_name = '/gpu:0'
        # else:
        #     device_name = '/cpu:0'
        # print('device name:', device_name)
        # with tf.device(device_name):

        # tf.reset_default_graph()
        seed = self.seed
        random.seed(seed)
        np.random.seed(seed)
        tf.set_random_seed(seed)

        ground_truth = tf.placeholder(tf.float32, [None, 1])
        mask = tf.placeholder(tf.float32, [None, 1])
        feature = tf.placeholder(tf.float32,
                                    [None, self.parameters['unit']])
        base_price = tf.placeholder(tf.float32, [None, 1])
        is_train=tf.placeholder(dtype=tf.bool,shape=[])
        all_one = tf.ones([tf.shape(feature)[0], 1], dtype=tf.float32)
        rel_shape = [self.rel_encoding.shape[0], self.rel_encoding.shape[1]]
        if self.reg=="part":
            part_label = tf.constant(self.part_label, dtype=tf.float32)
        if self.geom and self.unify=="2way":
            # original
            # for reg use
            rel_encoding = self.rel_encoding[:,:,:-1]
            mask_flags = np.equal(np.zeros(rel_shape, dtype=int),
                            np.sum(rel_encoding, axis=2))
            ori_mask = np.where(mask_flags, np.ones(rel_shape) * -1e9, np.zeros(rel_shape))
            
            relation = tf.constant(rel_encoding, dtype=tf.float32)
            rel_mask = tf.constant(ori_mask, dtype=tf.float32)
            rel_weight = tf.layers.dense(relation, units=1,name="rel",
                                            activation=leaky_relu)
            # structural
            stru_rel_encoding = self.rel_encoding[:,:,-1:]
            stru_mask_flags = np.equal(np.zeros(rel_shape, dtype=int),
                            np.sum(stru_rel_encoding, axis=2))
            stru_mask = np.where(stru_mask_flags, np.ones(rel_shape) * -1e9, np.zeros(rel_shape))

            stru_relation = tf.constant(stru_rel_encoding, dtype=tf.float32)
            stru_rel_mask = tf.constant(stru_mask, dtype=tf.float32)
            stru_rel_weight = tf.layers.dense(stru_relation, units=1,
                                            activation=leaky_relu)
        else:
            # total
            # for reg use
            rel_encoding = self.rel_encoding
            ori_mask = self.rel_mask
            relation = tf.constant(self.train_rel_encoding, dtype=tf.float32)
            rel_mask = tf.constant(self.train_rel_mask, dtype=tf.float32)
            all_relation = tf.constant(self.rel_encoding, dtype=tf.float32)
            all_rel_mask = tf.constant(self.rel_mask, dtype=tf.float32)
            relation = tf.cond(is_train , lambda:relation , lambda :all_relation)
            rel_mask  = tf.cond(is_train , lambda:rel_mask , lambda :all_rel_mask)
            rel_weight = tf.layers.dense(relation, units=1,name="rel",
                                            activation=leaky_relu)

        # original
        if self.inner_prod:
            print('inner product weight')
            inner_weight = tf.matmul(feature, feature, transpose_b=True)
            weight = tf.multiply(inner_weight, rel_weight[:, :, -1])
        else:
            print('sum weight')
            head_weight = tf.layers.dense(feature, units=1,name="head",
                                            activation=leaky_relu)
            tail_weight = tf.layers.dense(feature, units=1,name="tail",
                                            activation=leaky_relu)
            weight = tf.add(
                tf.add(
                    tf.matmul(head_weight, all_one, transpose_b=True),
                    tf.matmul(all_one, tail_weight, transpose_b=True)
                ), rel_weight[:, :, -1]
            )

        weight_masked = tf.nn.softmax(tf.add(rel_mask, weight), dim=0)
        outputs_proped = tf.matmul(weight_masked, feature)
        # 2way structural
        if self.geom and self.unify=="2way":
            if self.inner_prod:
                print('inner product weight')
                inner_weight = tf.matmul(feature, feature, transpose_b=True)
                weight = tf.multiply(inner_weight, rel_weight[:, :, -1])
            else:
                print('sum weight')
                head_weight = tf.layers.dense(feature, units=1,
                                                activation=leaky_relu)
                tail_weight = tf.layers.dense(feature, units=1,
                                                activation=leaky_relu)
                weight = tf.add(
                    tf.add(
                        tf.matmul(head_weight, all_one, transpose_b=True),
                        tf.matmul(all_one, tail_weight, transpose_b=True)
                    ), stru_rel_weight[:, :, -1]
                )
            weight_masked = tf.nn.softmax(tf.add(stru_rel_mask, weight), dim=0)
            stru_outputs_proped = tf.matmul(weight_masked, feature)
            outputs_proped = self.two_way_b*outputs_proped + (1-self.two_way_b)*stru_outputs_proped
        # unify
        if self.flat:
            print('one more hidden layer')
            outputs_concated = tf.layers.dense(
                tf.concat([feature, outputs_proped], axis=1),
                units=self.parameters['unit'], activation=leaky_relu,
                kernel_initializer=tf.glorot_uniform_initializer()
            )
        else:
            outputs_concated = tf.concat([feature, outputs_proped], axis=1)
        
        # One hidden layer
        prediction = tf.layers.dense(
            outputs_concated, units=1, activation=leaky_relu, name='reg_fc',
            kernel_initializer=tf.glorot_uniform_initializer()
        )
        # delete
        if self.reg=="reg":
            rel_del_encoding, rel_del_mask = rel_encoding.copy(), ori_mask.copy()
            zero = np.zeros((rel_del_encoding.shape[2]))
            for i in range(rel_del_encoding.shape[0]):
                rel_del_encoding[i,i,:] = zero
                rel_del_mask[i,i] = -1e9
            relation_del = tf.constant(rel_del_encoding, dtype=tf.float32)
            rel_del_mask = tf.constant(rel_del_mask, dtype=tf.float32)
            rel_del_weight = tf.layers.dense(relation_del, units=1,
                                            activation=leaky_relu, name="rel", reuse=True)
            head_weight = tf.layers.dense(feature, units=1,
                                                activation=leaky_relu,name="head",reuse=True)
            tail_weight = tf.layers.dense(feature, units=1,
                                            activation=leaky_relu,name="tail",reuse=True)
            weight = tf.add(
                tf.add(
                    tf.matmul(head_weight, all_one, transpose_b=True),
                    tf.matmul(all_one, tail_weight, transpose_b=True)
                ), rel_del_weight[:, :, -1]
            )

            weight_del_masked = tf.nn.softmax(tf.add(rel_del_mask, weight), dim=0)
            outputs_del_proped = tf.matmul(weight_del_masked, feature)
            regresion_loss = tf.reduce_mean(tf.sqrt(tf.reduce_sum((feature-outputs_del_proped)**2,axis=1)))
        # original loss
        return_ratio = tf.div(tf.subtract(prediction, base_price), base_price)
        reg_loss = tf.losses.mean_squared_error(
            ground_truth, return_ratio, weights=mask
        )
        pre_pw_dif = tf.subtract(
            tf.matmul(return_ratio, all_one, transpose_b=True),
            tf.matmul(all_one, return_ratio, transpose_b=True)
        )
        gt_pw_dif = tf.subtract(
            tf.matmul(all_one, ground_truth, transpose_b=True),
            tf.matmul(ground_truth, all_one, transpose_b=True)
        )
        mask_pw = tf.matmul(mask, mask, transpose_b=True)
        rank_loss = tf.reduce_mean(
            tf.nn.relu(
                tf.multiply(
                    tf.multiply(pre_pw_dif, gt_pw_dif),
                    mask_pw
                )
            )
        )
        
        loss = reg_loss + tf.cast(self.parameters['alpha'], tf.float32) * \
                            rank_loss
        if self.reg=="part":
            part_prediction = tf.layers.dense(
            outputs_concated, units=self.gp, activation=leaky_relu, name='reg_fc_part',
            kernel_initializer=tf.glorot_uniform_initializer()
            )
            part_loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.part_label,logits=part_prediction))
            loss+=self.reg_b*part_loss
        if self.reg=="reg":
            loss+=self.reg_b*regresion_loss

        optimizer = tf.train.AdamOptimizer(
            learning_rate=self.parameters['lr']
        ).minimize(loss)
        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        sess = tf.Session(config=config)
        saver = tf.train.Saver()
        sess.run(tf.global_variables_initializer())
        best_valid_pred = np.zeros(
            [len(self.tickers), self.test_index - self.valid_index],
            dtype=float
        )
        best_valid_gt = np.zeros(
            [len(self.tickers), self.test_index - self.valid_index],
            dtype=float
        )
        best_valid_mask = np.zeros(
            [len(self.tickers), self.test_index - self.valid_index],
            dtype=float
        )
        best_test_pred = np.zeros(
            [len(self.tickers), self.trade_dates - self.parameters['seq'] -
             self.test_index - self.steps + 1], dtype=float
        )
        best_test_gt = np.zeros(
            [len(self.tickers), self.trade_dates - self.parameters['seq'] -
             self.test_index - self.steps + 1], dtype=float
        )
        best_test_mask = np.zeros(
            [len(self.tickers), self.trade_dates - self.parameters['seq'] -
             self.test_index - self.steps + 1], dtype=float
        )
        best_valid_perf = {
            'mse': np.inf, 'mrrt': 0.0, 'btl': 0.0
        }
        best_test_perf = {
            'mse': np.inf, 'mrrt': 0.0, 'btl': 0.0
        }
        best_valid_loss = np.inf
        batch_offsets = np.arange(start=0, stop=self.valid_index, dtype=int)
        for i in range(self.epochs):
            t1 = time()
            np.random.seed(self.seed)
            np.random.shuffle(batch_offsets)
            tra_loss = 0.0
            tra_reg_loss = 0.0
            tra_rank_loss = 0.0
            for j in range(self.valid_index - self.parameters['seq'] -
                                   self.steps + 1):
                emb_batch, mask_batch, price_batch, gt_batch = self.get_train_batch(
                    batch_offsets[j])
                feed_dict = {
                    feature: emb_batch,
                    mask: mask_batch,
                    ground_truth: gt_batch,
                    base_price: price_batch,
                    is_train:True
                }
                cur_loss, cur_reg_loss, cur_rank_loss, batch_out = \
                    sess.run((loss, reg_loss, rank_loss, optimizer),
                             feed_dict)
                tra_loss += cur_loss
                tra_reg_loss += cur_reg_loss
                tra_rank_loss += cur_rank_loss
            print('Train Loss:',
                  tra_loss / (self.valid_index - self.parameters['seq'] - self.steps + 1),
                  tra_reg_loss / (self.valid_index - self.parameters['seq'] - self.steps + 1),
                  tra_rank_loss / (self.valid_index - self.parameters['seq'] - self.steps + 1))

            # test on validation set
            cur_valid_pred = np.zeros(
                [len(self.tickers), self.test_index - self.valid_index],
                dtype=float
            )
            cur_valid_gt = np.zeros(
                [len(self.tickers), self.test_index - self.valid_index],
                dtype=float
            )
            cur_valid_mask = np.zeros(
                [len(self.tickers), self.test_index - self.valid_index],
                dtype=float
            )
            val_loss = 0.0
            val_reg_loss = 0.0
            val_rank_loss = 0.0
            for cur_offset in range(
                self.valid_index - self.parameters['seq'] - self.steps + 1,
                self.test_index - self.parameters['seq'] - self.steps + 1
            ):
                emb_batch, mask_batch, price_batch, gt_batch = self.get_batch(
                    cur_offset)
                time2 = time()
                feed_dict = {
                    feature: emb_batch,
                    mask: mask_batch,
                    ground_truth: gt_batch,
                    base_price: price_batch,
                    is_train:False
                }
                cur_loss, cur_reg_loss, cur_rank_loss, cur_rr, = \
                    sess.run((loss, reg_loss, rank_loss,
                              return_ratio), feed_dict)
                time3 = time()
                val_loss += cur_loss
                val_reg_loss += cur_reg_loss
                val_rank_loss += cur_rank_loss
                cur_valid_pred[:, cur_offset - (self.valid_index -
                                                self.parameters['seq'] -
                                                self.steps + 1)] = \
                    copy.copy(cur_rr[:, 0])
                cur_valid_gt[:, cur_offset - (self.valid_index -
                                              self.parameters['seq'] -
                                              self.steps + 1)] = \
                    copy.copy(gt_batch[:, 0])
                cur_valid_mask[:, cur_offset - (self.valid_index -
                                                self.parameters['seq'] -
                                                self.steps + 1)] = \
                    copy.copy(mask_batch[:, 0])
            print('Valid MSE:',
                  val_loss / (self.test_index - self.valid_index),
                  val_reg_loss / (self.test_index - self.valid_index),
                  val_rank_loss / (self.test_index - self.valid_index))
            cur_valid_perf = evaluate(cur_valid_pred, cur_valid_gt,
                                      cur_valid_mask)
            print('\t Valid preformance:', cur_valid_perf)

            # test on testing set
            cur_test_pred = np.zeros(
                [len(self.tickers), self.trade_dates - self.test_index],
                dtype=float
            )
            cur_test_gt = np.zeros(
                [len(self.tickers), self.trade_dates - self.test_index],
                dtype=float
            )
            cur_test_mask = np.zeros(
                [len(self.tickers), self.trade_dates - self.test_index],
                dtype=float
            )
            test_loss = 0.0
            test_reg_loss = 0.0
            test_rank_loss = 0.0
            for cur_offset in range(
                                            self.test_index - self.parameters['seq'] - self.steps + 1,
                                            self.trade_dates - self.parameters['seq'] - self.steps + 1
            ):
                emb_batch, mask_batch, price_batch, gt_batch = self.get_batch(
                    cur_offset)
                feed_dict = {
                    feature: emb_batch,
                    mask: mask_batch,
                    ground_truth: gt_batch,
                    base_price: price_batch,
                    is_train:False
                }
                cur_loss, cur_reg_loss, cur_rank_loss, cur_rr = \
                    sess.run((loss, reg_loss, rank_loss,
                              return_ratio), feed_dict)
                test_loss += cur_loss
                test_reg_loss += cur_reg_loss
                test_rank_loss += cur_rank_loss

                cur_test_pred[:, cur_offset - (self.test_index -
                                               self.parameters['seq'] -
                                               self.steps + 1)] = \
                    copy.copy(cur_rr[:, 0])
                cur_test_gt[:, cur_offset - (self.test_index -
                                             self.parameters['seq'] -
                                             self.steps + 1)] = \
                    copy.copy(gt_batch[:, 0])
                cur_test_mask[:, cur_offset - (self.test_index -
                                               self.parameters['seq'] -
                                               self.steps + 1)] = \
                    copy.copy(mask_batch[:, 0])
            print('Test MSE:',
                  test_loss / (self.trade_dates - self.test_index),
                  test_reg_loss / (self.trade_dates - self.test_index),
                  test_rank_loss / (self.trade_dates - self.test_index))
            cur_test_perf = evaluate(cur_test_pred, cur_test_gt, cur_test_mask)
            print('\t Test performance:', cur_test_perf)
            if val_loss / (self.test_index - self.valid_index) < \
                    best_valid_loss:
                best_valid_loss = val_loss / (self.test_index -
                                              self.valid_index)
                best_valid_perf = copy.copy(cur_valid_perf)
                best_valid_gt = copy.copy(cur_valid_gt)
                best_valid_pred = copy.copy(cur_valid_pred)
                best_valid_mask = copy.copy(cur_valid_mask)
                best_test_perf = copy.copy(cur_test_perf)
                best_test_gt = copy.copy(cur_test_gt)
                best_test_pred = copy.copy(cur_test_pred)
                best_test_mask = copy.copy(cur_test_mask)
                print('Better valid loss:', best_valid_loss)
            t4 = time()
            print('epoch:', i, ('time: %.4f ' % (t4 - t1)))
        print('\nBest Valid performance:', best_valid_perf)
        print('\tBest Test performance:', best_test_perf)
        logging.info('\tBest Test performance:'+ str(best_test_perf))
        sess.close()
        tf.reset_default_graph()
        return best_valid_pred, best_valid_gt, best_valid_mask, \
               best_test_pred, best_test_gt, best_test_mask

    def update_model(self, parameters):
        for name, value in parameters.items():
            self.parameters[name] = value
        return True


if __name__ == '__main__':
    desc = 'train a relational rank lstm model'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('-p', help='path of EOD data',
                        default='../data/2013-01-01')
    parser.add_argument('-m', help='market name', default='NASDAQ')
    parser.add_argument('-t', help='fname for selected tickers')
    parser.add_argument('-l', default=4,
                        help='length of historical sequence for feature')
    parser.add_argument('-u', default=64,
                        help='number of hidden units in lstm')
    parser.add_argument('-s', default=1,
                        help='steps to make prediction')
    parser.add_argument('-r', default=0.001,
                        help='learning rate')
    parser.add_argument('-a', default=1,
                        help='alpha, the weight of ranking loss')
    parser.add_argument('-train_ratio', default=0.1,
                        help='train ratio of stocks')
    parser.add_argument('-train_ratio_seed', default=0,
                        help='train ratio of stocks')                  
    parser.add_argument('-g', '--gpu', type=int, default=0, help='use gpu')
    parser.add_argument('-e', '--emb_file', type=str,
                        default='NASDAQ_rank_lstm_seq-16_unit-64_2.csv.npy',
                        help='fname for pretrained sequential embedding')
    parser.add_argument('-rn', '--rel_name', type=str,
                        default='sector_industry',
                        help='relation type: sector_industry or wikidata')
    parser.add_argument('-ip', '--inner_prod', type=int, default=0)
    parser.add_argument('-epoch', '--epoch', type=int, default=50)
    parser.add_argument('-geom', action='store_true')
    parser.add_argument('-unify', type=str, default="add",
                            help='self supervised loss type: add or 2way')
    parser.add_argument('-two_way_b', type=float, default=0.95,
                            help='unify parameter for 2way')
    parser.add_argument('-self', type=str, default="None",
                help='self supervised loss type: part or reg')
    parser.add_argument('-thresh', type=float, default=1.51,
                help='threshold')
    parser.add_argument('-gp', type=int, default=32,
                help='number of graph partitions for loss part')
    parser.add_argument('-self_b', type=float, default=1e-4)
    args = parser.parse_args()

    if args.t is None:
        args.t = args.m + '_tickers_qualify_dr-0.98_min-5_smooth.csv'
    os.environ["CUDA_VISIBLE_DEVICES"]=str(args.gpu)
    parameters = {'seq': int(args.l), 'unit': int(args.u), 'lr': float(args.r),
                  'alpha': float(args.a)}
    print('arguments:', args)
    print('parameters:', parameters)
    
    args.inner_prod = (args.inner_prod == 1)
    seeds = list(range(5))
    # seeds = [3,4]
    logging.basicConfig(filename='few_log/{}_ratio_{}_seeds_{}.log'.format(args.m, args.train_ratio, args.train_ratio_seed), level=logging.INFO)
    
    logging.info(" ")
    for seed in seeds:
        tf.reset_default_graph()
        np.random.seed(seed)
        tf.set_random_seed(seed)
        RR_LSTM = ReRaLSTM(
            data_path=args.p,
            market_name=args.m,
            tickers_fname=args.t,
            relation_name=args.rel_name,
            emb_fname=args.emb_file,
            parameters=parameters,
            steps=1, epochs=args.epoch, batch_size=None,
            in_pro=args.inner_prod,
            seed=seed,
            geom=args.geom,
            args=args
        )
        pred_all = RR_LSTM.train()