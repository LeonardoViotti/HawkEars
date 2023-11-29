# Model training

import argparse
import logging
import os
import random
import time

from core import cfg, configs, data_module, set_config
from model import main_model

import numpy as np
import pytorch_lightning as pl
from pytorch_lightning.callbacks import LearningRateMonitor, ModelCheckpoint, TQDMProgressBar
from pytorch_lightning.loggers import TensorBoardLogger
import torch
import torch.nn.functional as F

class Trainer:
    def __init__(self):
        torch.set_float32_matmul_precision('medium') # may improve performance a little
        pl.seed_everything(cfg.train.seed)

    def run(self):
        # load all the data once for performance, then split as needed in each fold
        dm = data_module.DataModule()
        dm.load_data()
        weights = dm.class_weights()

        for k in range(cfg.train.num_folds):
            trainer = pl.Trainer(
                accelerator='auto',
                callbacks=[ModelCheckpoint(save_top_k=cfg.train.save_last_n, mode='max', monitor='epoch_num'),
                           TQDMProgressBar(refresh_rate=10)],
                deterministic=cfg.train.deterministic,
                devices=1 if torch.cuda.is_available() else None,
                max_epochs=cfg.train.num_epochs,
                logger=TensorBoardLogger(save_dir='logs', name=f'fold-{k}', default_hp_metric=False),
            )

            dm.prepare_fold(k, cfg.train.num_folds)

            # create model inside loop so parameters are reset for each fold,
            # and so metrics are tracked correctly
            if cfg.train.load_ckpt_path is not None:
                logging.info(f"Loading model from {cfg.train.load_ckpt_path}")
                model = main_model.MainModel.load_from_checkpoint(cfg.train.load_ckpt_path)
                if cfg.train.freeze_backbone:
                    model.freeze()

                if cfg.train.update_classifier:
                    model.update_classifier(dm.train_class_names, dm.train_class_codes, dm.test_class_names, weights)
                else:
                    model.unfreeze_classifier()
            else:
                model = main_model.MainModel(dm.train_class_names, dm.train_class_codes, dm.test_class_names, weights,
                                             cfg.train.model_name, cfg.train.load_weights, dm.num_train_specs)

            if cfg.train.compile:
                # skip compile for short tests
                model = torch.compile(model)

            trainer.fit(model, dm)

            if cfg.misc.test_pickle is not None:
                trainer.test(model, dm)

if __name__ == '__main__':
    # command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=str, default='base', help=f"Configuration name. Default = 'base'.")
    parser.add_argument('-de', '--debug', default=False, action='store_true', help="Flag for debug mode.")
    parser.add_argument('-e', '--epochs', type=int, default=None, help=f"Number of epochs.")
    args = parser.parse_args()

    cfg_name = args.config
    if cfg_name in configs:
        set_config(cfg_name)
    else:
        print(f"Configuration '{cfg_name}' not found.")
        quit()

    if args.epochs is not None:
        cfg.train.num_epochs = args.epochs

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S")
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S")

    start_time = time.time()
    Trainer().run()
    elapsed = time.time() - start_time
    logging.info(f"Elapsed time = {int(elapsed) // 60}m {int(elapsed) % 60}s\n")
