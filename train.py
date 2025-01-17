#!/usr/bin/python
# -*- encoding: utf-8 -*-
from torchinfo import summary
from timeit import default_timer as timer
from datasets.gta import Gta
from model.model_stages import BiSeNet
from datasets.cityscapes import CityScapes
import torch
from torch.utils.data import DataLoader, random_split
import logging
import argparse
import numpy as np
from tensorboardX import SummaryWriter
import torch.cuda.amp as amp
from utils import poly_lr_scheduler
from utils import reverse_one_hot, compute_global_accuracy, fast_hist, per_class_iu
from tqdm import tqdm
from model.discriminator import Discriminator, DiagonalwiseDiscriminator, DepthwiseDiscriminator

logger = logging.getLogger()


def val(args, model, dataloader):
    print('start val!')
    with torch.no_grad():
        model.eval()
        precision_record = []
        hist = np.zeros((args.num_classes, args.num_classes))
        for i, (data, label) in enumerate(dataloader):
            label = label.type(torch.LongTensor)
            data = data.cuda()
            label = label.long().cuda()

            # get RGB predict image
            predict, _, _ = model(data)
            predict = predict.squeeze(0)
            predict = reverse_one_hot(predict)
            predict = np.array(predict.cpu())

            # get RGB label image
            label = label.squeeze()
            label = np.array(label.cpu())

            # compute per pixel accuracy
            precision = compute_global_accuracy(predict, label)
            hist += fast_hist(label.flatten(), predict.flatten(), args.num_classes)

            # there is no need to transform the one-hot array to visual RGB array
            # predict = colour_code_segmentation(np.array(predict), label_info)
            # label = colour_code_segmentation(np.array(label), label_info)
            precision_record.append(precision)

        precision = np.mean(precision_record)
        miou_list = per_class_iu(hist)
        miou = np.mean(miou_list)
        print('precision per pixel for test: %.3f' % precision)
        print('mIoU for validation: %.3f' % miou)
        print(f'mIoU per class: {miou_list}')

        return precision, miou


def train(args, model, optimizer, dataloader_train, dataloader_val):
    writer = SummaryWriter(logdir=args.tensorboard_path, comment=''.format(args.optimizer))

    scaler = amp.GradScaler()

    # se ho capito bene, il 255 è il valore che rappresenta la classe void e che quindi
    # non deve essere considerato nella loss function.
    loss_func = torch.nn.CrossEntropyLoss(ignore_index=255)
    max_miou = 0
    step = 0
    train_times = []

    for epoch in range(args.num_epochs):
        train_time_start = timer()
        lr = poly_lr_scheduler(optimizer, args.learning_rate, iter=epoch, max_iter=args.num_epochs)
        model.train()
        tq = tqdm(total=len(dataloader_train) * args.batch_size)
        tq.set_description('epoch %d, lr %f' % (epoch, lr))
        loss_record = []
        for i, (data, label) in enumerate(dataloader_train):
            data = data.cuda()
            label = label.long().cuda()
            optimizer.zero_grad()
            print(f'data shape: {data.shape}')

            with amp.autocast():
                output, out16, out32 = model(data)
                loss1 = loss_func(output, label.squeeze(1))
                loss2 = loss_func(out16, label.squeeze(1))
                loss3 = loss_func(out32, label.squeeze(1))
                loss = loss1 + loss2 + loss3

            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            tq.update(args.batch_size)
            tq.set_postfix(loss='%.6f' % loss)
            step += 1
            writer.add_scalar('loss_step', loss, step)
            loss_record.append(loss.item())
        tq.close()
        loss_train_mean = np.mean(loss_record)
        writer.add_scalar('epoch/loss_epoch_train', float(loss_train_mean), epoch)
        print('loss for train : %f' % (loss_train_mean))
        if epoch % args.checkpoint_step == 0 and epoch != 0:
            import os
            if not os.path.isdir(args.save_model_path):
                os.mkdir(args.save_model_path)
            torch.save(model.module.state_dict(), os.path.join(args.save_model_path, 'latest.pth'))

        if epoch % args.validation_step == 0 and epoch != 0:
            precision, miou = val(args, model, dataloader_val)
            if miou > max_miou:
                max_miou = miou
                import os
                os.makedirs(args.save_model_path, exist_ok=True)
                torch.save(model.module.state_dict(), os.path.join(args.save_model_path, 'best.pth'))
            writer.add_scalar('epoch/precision_val', precision, epoch)
            writer.add_scalar('epoch/miou val', miou, epoch)
        train_time_end = timer()
        train_times.append(train_time_end - train_time_start)

    print(f'Average train time per epoch in minutes: {np.mean(train_times) / 60}')


def train_adversarial(args, G, D, optimizer_G, optimizer_D, dataloader_gta5, dataloader_cityscapes,
                      dataloader_val_cityscapes):
    lambda_adv = 0.001  # Define the weight of the adversarial loss
    lambda_seg = 1  # Define the weight of the segmentation loss
    # torch.autograd.set_detect_anomaly(True)
    writer = SummaryWriter(logdir=args.tensorboard_path, comment=''.format(args.optimizer))

    scaler = amp.GradScaler()  # Initialize gradient scaler for mixed precision training

    loss_func_seg = torch.nn.CrossEntropyLoss(ignore_index=255)  # Define the loss function for the segmentation model
    loss_func_d = torch.nn.BCEWithLogitsLoss()  # Define the loss function for the discriminator
    loss_func_adv = torch.nn.BCEWithLogitsLoss()  # Define the loss function for the adversarial loss

    max_miou = 0  # Variable to store the maximum mean IoU
    step = 0  # Variable to count training steps
    train_times = []

    for epoch in range(args.num_epochs):
        total_time = 0
        train_time_start = timer()
        lr_G = poly_lr_scheduler(optimizer_G, args.learning_rate, iter=epoch,
                                 max_iter=args.num_epochs)  # Update learning rate
        lr_D = poly_lr_scheduler(optimizer_D, args.discriminator_learning_rate, iter=epoch,
                                 max_iter=args.num_epochs)  # Update learning rate
        tq = tqdm(
            total=len(dataloader_cityscapes) * args.batch_size)  # Initialize tqdm progress bar
        tq.set_description('epoch %d, lr_G %f,  lr_D %f' % (epoch, lr_G, lr_D))
        loss_record = []  # List to record loss values

        for i, (data_gta5, data_cityscapes) in enumerate(zip(dataloader_gta5, dataloader_cityscapes)):
            data_gta5, label_gta5 = data_gta5  # Unpack GTA5 datasets
            data_gta5 = data_gta5.cuda()  # Move GTA5 images to GPU
            label_gta5 = label_gta5.long().cuda()  # Move GTA5 labels to GPU
            data_cityscapes, _ = data_cityscapes  # Unpack Cityscapes datasets
            data_cityscapes = data_cityscapes.cuda()  # Move Cityscapes datasets to GPU
            optimizer_G.zero_grad()  # Zero the gradients
            optimizer_D.zero_grad()  # Zero the gradients

            G.train()  # Set the model to training mode
            D.train()  # Set the model to training mode

            # Train the segmentation model with GTA5 datasets
            with amp.autocast():
                output_gta5, out16_gta5, out32_gta5 = G(data_gta5)  # Get predictions from the model at multiple scales
                # Calculate loss at multiple scales
                loss1_gta5 = loss_func_seg(output_gta5, label_gta5.squeeze(1))
                loss2_gta5 = loss_func_seg(out16_gta5, label_gta5.squeeze(1))
                loss3_gta5 = loss_func_seg(out32_gta5, label_gta5.squeeze(1))
                loss_seg = loss1_gta5 + loss2_gta5 + loss3_gta5  # Combine losses

            scaler.scale(loss_seg).backward()  # Scale loss and perform backpropagation
            scaler.step(optimizer_G)  # Perform optimizer step
            scaler.update()

            with amp.autocast():
                # Get predictions from the segmentation model on Cityscapes datasets
                output_cityscapes, _, _ = G(data_cityscapes)
            optimizer_G.zero_grad()  # Zero the gradients

            for param in D.parameters():
                param.requires_grad = False

            with amp.autocast():
                # Forward pass of Cityscapes datasets through the discriminator
                d_cityscapes = D(output_cityscapes)
                d_label_gta5 = torch.ones(d_cityscapes.size(0), 1, d_cityscapes.size(2),
                                                 d_cityscapes.size(
                                                     3)).cuda()  # Labels are 1 for GTA5 datasets

                # the adversarial loss is calculated on the target prediction
                loss_adv_cityscapes = loss_func_adv(d_cityscapes, d_label_gta5)

            loss_adv = loss_adv_cityscapes * lambda_adv

            # the adv loss is back-propagated to the segmentation network G and not to the discriminator D
            scaler.scale(loss_adv).backward()
            scaler.step(optimizer_G)
            scaler.update()

            # Combine segmentation and adversarial losses (Lseg(Is) + λLadv(It)
            total_loss = loss_seg + loss_adv

            # bring back requires_grad
            for param in D.parameters():
                param.requires_grad = True

            with amp.autocast():
                # Forward pass of GTA5 datasets through the discriminator
                train_time_start_prova = timer()
                d_gta5 = D(output_gta5.detach())
                train_time_end_prova = timer()

                # Calculate loss for GTA5 datasets
                loss_d_gta5 = loss_func_d(d_gta5, d_label_gta5)

            scaler.scale(loss_d_gta5).backward()  # Scale loss and perform backpropagation
            scaler.step(optimizer_D)  # Perform optimizer step
            scaler.update()

            with amp.autocast():
                # Forward pass of Cityscapes datasets through the discriminator
                d_cityscapes = D(output_cityscapes.detach())
                
                d_label_cityscapes = torch.zeros(d_cityscapes.size(0), 1, d_cityscapes.size(2),
                                                 d_cityscapes.size(
                                                     3)).cuda()  # Labels are 0 for Cityscapes datasets

                # Calculate loss for Cityscapes datasets
                loss_d_cityscapes = loss_func_d(d_cityscapes, d_label_cityscapes)

            optimizer_D.zero_grad()  # Zero the gradients
            scaler.scale(loss_d_cityscapes).backward()  # Scale loss and perform backpropagation
            scaler.step(optimizer_D)  # Perform optimizer step
            scaler.update()

            time = train_time_end_prova - train_time_start_prova
            total_time = total_time + time
            tq.update(args.batch_size)
            tq.set_postfix(loss_seg='%.6f' % loss_seg, loss_adv='%.6f' % loss_adv, loss_cs='%.6f' % loss_d_cityscapes,
                           loss_gta='%.6f' % loss_d_gta5, total_loss='%.6f' % total_loss, atime='%.6f' % time,
                           abtime='%.6f' % (total_time / float(i + 1)))
            step += 1
            writer.add_scalar('seg_loss_step', loss_seg, step)
            writer.add_scalar('adv_loss_step', loss_adv, step)
            writer.add_scalar('loss_step', total_loss, step)
            loss_record.append(total_loss.item())

        tq.close()
        loss_train_mean = np.mean(loss_record)
        writer.add_scalar('epoch/loss_epoch_train', float(loss_train_mean), epoch)

        print('loss for train : %f' % (loss_train_mean))
        if epoch % args.checkpoint_step == 0 and epoch != 0:
            import os
            if not os.path.isdir(args.save_model_path):
                os.mkdir(args.save_model_path)
            torch.save(G.module.state_dict(), os.path.join(args.save_model_path, 'G_latest.pth'))
            torch.save(D.module.state_dict(), os.path.join(args.save_model_path, 'D_latest.pth'))

        if epoch % args.validation_step == 0 and epoch != 0:
            precision, miou = val(args, G, dataloader_val_cityscapes)
            if miou > max_miou:
                max_miou = miou
                import os
                os.makedirs(args.save_model_path, exist_ok=True)
                torch.save(G.module.state_dict(), os.path.join(args.save_model_path, 'G_best.pth'))
            writer.add_scalar('epoch/precision_val', precision, epoch)
            writer.add_scalar('epoch/miou val', miou, epoch)
        train_time_end = timer()
        train_times.append(train_time_end - train_time_start)

    print(f'Average train time per epoch in minutes: {np.mean(train_times) / 60}')


def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Unsupported value encountered.')


def parse_args():
    parse = argparse.ArgumentParser()

    parse.add_argument('--mode',
                       dest='mode',
                       type=str,
                       default='train',
                       )
    parse.add_argument('--backbone',
                       dest='backbone',
                       type=str,
                       default='CatmodelSmall',
                       )
    parse.add_argument('--depthwise_discriminator',
                       dest='depthwise_discriminator',
                       type=str,
                       default='',
                       )
    parse.add_argument('--train_dataset',
                       dest='train_dataset',
                       type=str,
                       default='Cityscapes',
                       )
    parse.add_argument('--val_dataset',
                       dest='val_dataset',
                       type=str,
                       default='Cityscapes',
                       )
    parse.add_argument('--pretrain_path',
                       dest='pretrain_path',
                       type=str,
                       default='',
                       )
    parse.add_argument('--save_model_path',
                       type=str,
                       default=None,
                       help='path to save model')
    parse.add_argument('--use_conv_last',
                       dest='use_conv_last',
                       type=str2bool,
                       default=False,
                       )
    parse.add_argument('--num_epochs',
                       type=int,
                       default=300,
                       help='Number of epochs to train for')
    parse.add_argument('--epoch_start_i',
                       type=int,
                       default=0,
                       help='Start counting epochs from this number')
    parse.add_argument('--checkpoint_step',
                       type=int,
                       default=10,
                       help='How often to save checkpoints (epochs)')
    parse.add_argument('--validation_step',
                       type=int,
                       default=1,
                       help='How often to perform validation (epochs)')
    parse.add_argument('--crop_height',
                       type=int,
                       default=512,
                       help='Height of cropped/resized input image to modelwork')
    parse.add_argument('--crop_width',
                       type=int,
                       default=1024,
                       help='Width of cropped/resized input image to modelwork')
    parse.add_argument('--batch_size',
                       type=int,
                       default=2,
                       help='Number of images in each batch')
    parse.add_argument('--learning_rate',
                       type=float,
                       default=0.01,
                       help='learning rate used for train')
    parse.add_argument('--discriminator_learning_rate',
                       type=float,
                       default=0.01,
                       help='learning rate used for discriminator train')
    parse.add_argument('--num_workers',
                       type=int,
                       default=4,
                       help='num of workers')
    parse.add_argument('--num_classes',
                       type=int,
                       default=19,
                       help='num of object classes (with void)')
    parse.add_argument('--cuda',
                       type=str,
                       default='0',
                       help='GPU ids used for training')
    parse.add_argument('--use_gpu',
                       type=str2bool,
                       default=True,
                       help='whether to user gpu for training')
    parse.add_argument('--tensorboard_path',
                       type=str,
                       default='runs',
                       help='path to save graph for TensorBoard')
    parse.add_argument('--optimizer',
                       type=str,
                       default='adam',
                       help='optimizer, support rmsprop, sgd, adam')
    parse.add_argument('--loss',
                       type=str,
                       default='crossentropy',
                       help='loss function')

    return parse.parse_args()


def main():
    args = parse_args()

    n_classes = args.num_classes
    mode = args.mode
    torch.manual_seed(42)

    # model
    model = BiSeNet(backbone=args.backbone, n_classes=n_classes, pretrain_model=args.pretrain_path,
                    use_conv_last=args.use_conv_last)

    if mode == 'train':

        # dataset class
        if args.train_dataset == 'Cityscapes' and args.val_dataset == 'Cityscapes':

            train_dataset = CityScapes(mode, transformations=True, args=args)
            val_dataset = CityScapes(mode='val', transformations=True, args=args)

        elif args.train_dataset == 'GTA' and args.val_dataset == 'GTA':

            dataset = Gta(transformations=True, args=args)
            # Supponi di avere un dataset 'dataset'
            dataset_size = len(dataset)
            train_size = int(dataset_size * 0.8)  # 80% per l'addestramento
            test_size = dataset_size - train_size  # Il resto per il test

            # Suddividi il dataset
            train_dataset, val_dataset = random_split(dataset, [train_size, test_size])

        elif args.train_dataset == 'GTA_aug' and args.val_dataset == 'GTA':

            dataset = Gta(transformations=True, args=args)
            # Supponi di avere un dataset 'dataset'
            dataset_size = len(dataset)
            train_size = int(dataset_size * 0.8)  # 80% per l'addestramento
            test_size = dataset_size - train_size  # Il resto per il test

            # Suddividi il dataset
            train_dataset, val_dataset = random_split(dataset, [train_size, test_size])
            train_dataset.set_augmentation(True)

        elif args.train_dataset == 'GTA_aug' and args.val_dataset == 'Cityscapes':
            train_dataset = Gta(transformations=True, data_augmentation=True, args=args)
            val_dataset = CityScapes(mode='val', transformations=True, args=args)
        else:
            raise ValueError('Dataset not supported')

        # dataloader class
        dataloader_train = DataLoader(train_dataset,
                                      batch_size=args.batch_size,
                                      shuffle=True,
                                      num_workers=args.num_workers,
                                      pin_memory=False,
                                      drop_last=True)

        dataloader_val = DataLoader(val_dataset,
                                    batch_size=1,
                                    shuffle=True,
                                    num_workers=args.num_workers,
                                    drop_last=False)

        # optimizer
        if args.optimizer == 'rmsprop':
            optimizer = torch.optim.RMSprop(model.parameters(), args.learning_rate)
        elif args.optimizer == 'sgd':
            optimizer = torch.optim.SGD(model.parameters(), args.learning_rate, momentum=0.9, weight_decay=5e-4)
        elif args.optimizer == 'adam':
            optimizer = torch.optim.Adam(model.parameters(), args.learning_rate)
        else:
            print('not supported optimizer \n')
            return None

        # load model to gpu
        if torch.cuda.is_available() and args.use_gpu:
            model = torch.nn.DataParallel(model).cuda()

        # train loop
        train(args, model, optimizer, dataloader_train, dataloader_val)

        # final test
        val(args, model, dataloader_val)

    elif mode == 'train_adversarial':

        cityscapes_train_dataset = CityScapes(mode='train', transformations=True, args=args)
        cityscapes_val_dataset = CityScapes(mode='val', transformations=True, args=args)
        GTA_full = Gta(transformations=True, data_augmentation=True, args=args)

        # dataloader class
        cityscapes_dataloader_train = DataLoader(cityscapes_train_dataset,
                                                 batch_size=args.batch_size,
                                                 shuffle=True,
                                                 num_workers=args.num_workers,
                                                 pin_memory=False,
                                                 drop_last=True)

        cityscapes_dataloader_val = DataLoader(cityscapes_val_dataset,
                                               batch_size=1,
                                               shuffle=True,
                                               num_workers=args.num_workers,
                                               drop_last=False)

        GTA_dataloader = DataLoader(GTA_full,
                                    batch_size=args.batch_size,
                                    shuffle=True,
                                    num_workers=args.num_workers,
                                    pin_memory=False,
                                    drop_last=True)

        # create Discriminator class
        if args.depthwise_discriminator == 'depthwise':
            discriminator = DepthwiseDiscriminator(in_channels=n_classes)
        elif args.depthwise_discriminator == 'diagonalwise':
            discriminator = DiagonalwiseDiscriminator(in_channels=n_classes)
        else:
            discriminator = Discriminator(in_channels=n_classes)

        # optimizers
        optimizer_G = torch.optim.SGD(model.parameters(), lr=args.learning_rate, momentum=0.9,
                                      weight_decay=5e-4)
        optimizer_D = torch.optim.Adam(discriminator.parameters(), lr=args.discriminator_learning_rate)

        # load model to gpu
        if torch.cuda.is_available() and args.use_gpu:
            model = torch.nn.DataParallel(model).cuda()
            discriminator = torch.nn.DataParallel(discriminator).cuda()

        # train loop
        train_adversarial(args, G=model, D=discriminator, optimizer_G=optimizer_G, optimizer_D=optimizer_D,
                          dataloader_gta5=GTA_dataloader,
                          dataloader_cityscapes=cityscapes_dataloader_train,
                          dataloader_val_cityscapes=cityscapes_dataloader_val)

        # final test
        val(args, model, cityscapes_dataloader_val)

    elif mode == 'val':

        if args.val_dataset == 'Cityscapes':
            val_dataset = CityScapes(mode='val', transformations=True, args=args)
        elif args.val_dataset == 'GTA':
            val_dataset = Gta(transformations=True, args=args)
        else:
            raise ValueError('Dataset not supported')

        dataloader_val = DataLoader(val_dataset,
                                    batch_size=1,
                                    shuffle=True,
                                    num_workers=args.num_workers,
                                    drop_last=False)
        if args.save_model_path is not None:
            # Load in the saved state_dict()
            model.load_state_dict(torch.load(f=args.save_model_path))
        else:
            raise ValueError('save_model_path must be specified')

        # load model to gpu
        if torch.cuda.is_available() and args.use_gpu:
            model = torch.nn.DataParallel(model).cuda()

        # final test
        val(args, model, dataloader_val)


if __name__ == "__main__":
    main()

#    model = BiSeNet(backbone='CatmodelSmall', n_classes=19)
#    summary(model=model,
#            input_size=(8, 3, 1024, 512),
#            col_names=["input_size", "output_size", "num_params", "trainable"],
#            col_width=20,
#            row_settings=["var_names"]
#            )
