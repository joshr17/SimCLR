import argparse
import warnings

import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
from torchvision import datasets
from tqdm import tqdm

import utils
from model import Net

warnings.filterwarnings("ignore")


def get_shuffle_idx(num_data):
    """shuffle index for ShuffleBN """
    shuffle_value = torch.randperm(num_data)
    reverse_idx = torch.zeros(num_data).long()
    arrange_index = torch.arange(num_data)
    reverse_idx.index_copy_(0, shuffle_value, arrange_index)
    return shuffle_value, reverse_idx


def train(model_q, model_k, train_loader, optimizer, epoch, temp=0.07):
    model_q.train()
    total_loss, n_data, train_bar = 0, 0, tqdm(train_loader)
    queue = torch.zeros((0, features_dim)).to('cuda')
    for data, target in train_bar:
        x_q, x_k = data, data.clone().detach()
        x_q, x_k = x_q.to('cuda'), x_k.to('cuda')
        N, K = x_q.shape[0], queue.shape[0]

        q = model_q(x_q)

        # shuffle BN
        shuffle_idx, reverse_idx = get_shuffle_idx(N)
        x_k = x_k[shuffle_idx.to('cuda')]
        k = model_k(x_k).detach()
        k = k[reverse_idx.to('cuda')]

        if K >= dictionary_size:
            l_pos = torch.bmm(q.view(N, 1, -1), k.view(N, -1, 1))
            l_neg = torch.mm(q, queue.t().contiguous())

            logits = torch.cat([l_pos.view(N, 1), l_neg], dim=-1)
            labels = torch.zeros(N, dtype=torch.long).to('cuda')
            loss = cross_entropy_loss(logits / temp, labels)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            n_data += N
            total_loss += loss.item() * N

            utils.momentum_update(model_q, model_k)
            train_bar.set_description('Train Epoch: [{}/{}] Loss: {:.6f}'.format(epoch, epochs, total_loss / n_data))

        queue = utils.queue_data(queue, k)
        queue = utils.dequeue_data(queue, dictionary_size)

    return total_loss / n_data


def test(model, train_loader, test_loader, epoch):
    model.eval()
    total_top1, total_top5, n_data, memory_bank = 0, 0, 0, []
    train_bar, test_bar = tqdm(train_loader, desc='Feature extracting'), tqdm(test_loader)
    with torch.no_grad():
        for data, target in train_bar:
            memory_bank.append(model(data.to('cuda')))
        memory_bank = torch.cat(memory_bank).t().contiguous()
        memory_bank_labels = torch.tensor(train_loader.dataset.targets).to('cuda')
        for data, target in test_bar:
            y = model(data.to('cuda'))
            n_data += len(data)
            sim_index = torch.mm(y, memory_bank).argsort(dim=-1, descending=True)[:, :min(memory_bank.size(-1), 200)]
            sim_labels = torch.index_select(memory_bank_labels, dim=-1, index=sim_index.reshape(-1)).view(len(data), -1)
            pred_labels = []
            for sim_label in sim_labels:
                pred_labels.append(torch.histc(sim_label.float(), bins=len(train_loader.dataset.classes)))
            pred_labels = torch.stack(pred_labels).argsort(dim=-1, descending=True)
            total_top1 += torch.sum(
                (pred_labels[:, :1] == target.to('cuda').unsqueeze(dim=-1)).any(dim=-1).float()).cpu().item()
            total_top5 += torch.sum(
                (pred_labels[:, :5] == target.to('cuda').unsqueeze(dim=-1)).any(dim=-1).float()).cpu().item()
            test_bar.set_description('Test Epoch: [{}/{}] Acc@1:{:.2f}% Acc@5:{:.2f}%'
                                     .format(epoch, epochs, total_top1 / n_data * 100, total_top5 / n_data * 100))

    return total_top1 / n_data * 100, total_top5 / n_data * 100


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train MoCo')
    parser.add_argument('--data_path', type=str, default='/home/data/imagenet/ILSVRC2012', help='Path to dataset')
    parser.add_argument('--model_type', default='resnet18', type=str,
                        choices=['resnet18', 'resnet50'], help='Backbone type')
    parser.add_argument('--batch_size', type=int, default=256, help='Number of images in each mini-batch')
    parser.add_argument('--epochs', type=int, default=200, help='Number of sweeps over the dataset to train')
    parser.add_argument('--features_dim', type=int, default=128, help='Dim of features for each image')
    parser.add_argument('--dictionary_size', type=int, default=65536, help='Size of dictionary')

    args = parser.parse_args()
    batch_size, epochs, features_dim, data_path = args.batch_size, args.epochs, args.features_dim, args.data_path
    dictionary_size, model_type = args.dictionary_size, args.model_type
    train_data = datasets.CIFAR10(root='data', train=True, transform=utils.train_transform, download=True)
    train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True, num_workers=8)
    train_test_data = datasets.CIFAR10(root='data', train=True, transform=utils.test_transform, download=True)
    train_test_loader = DataLoader(train_test_data, batch_size=batch_size, shuffle=False, num_workers=8)
    test_data = datasets.CIFAR10(root='data', train=False, transform=utils.test_transform, download=True)
    test_loader = DataLoader(test_data, batch_size=batch_size, shuffle=False, num_workers=8)

    model_q = Net(model_type, features_dim).to('cuda')
    model_k = Net(model_type, features_dim).to('cuda')
    for param in model_k.parameters():
        param.requires_grad = False
    utils.momentum_update(model_q, model_k, beta=0.0)
    optimizer = optim.SGD(model_q.parameters(), lr=0.03, momentum=0.9, weight_decay=0.0001)
    print("# trainable parameters:", sum(param.numel() if param.requires_grad else 0 for param in model_q.parameters()))
    lr_scheduler = MultiStepLR(optimizer, milestones=[int(epochs * 0.6), int(epochs * 0.8)], gamma=0.1)
    cross_entropy_loss = nn.CrossEntropyLoss()
    results = {'train_loss': [], 'test_acc@1': [], 'test_acc@5': []}

    best_acc = 0
    for epoch in range(1, epochs + 1):
        current_loss = train(model_q, model_k, train_loader, optimizer, epoch)
        results['train_loss'].append(current_loss)
        current_acc_1, current_acc_5 = test(model_q, train_test_loader, test_loader, epoch)
        results['test_acc@1'].append(current_acc_1)
        results['test_acc@5'].append(current_acc_5)
        # save statistics
        data_frame = pd.DataFrame(data=results, index=range(1, epoch + 1))
        data_frame.to_csv('results/{}_{}_{}_results.csv'.format(model_type, features_dim, dictionary_size),
                          index_label='epoch')
        lr_scheduler.step(epoch)
        if current_acc_1 > best_acc:
            best_acc = current_acc_1
            torch.save(model_q.state_dict(), 'epochs/{}_{}_{}.pth'.format(model_type, features_dim, dictionary_size))
