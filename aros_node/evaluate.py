import numpy as np
from tqdm.notebook import tqdm
from sklearn.metrics import roc_curve, roc_auc_score, precision_recall_curve, auc
from aros_node.utils import *
import argparse
import torch
import torch.nn as nn
import torchvision
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from sklearn.mixture import GaussianMixture
import numpy as np
from scipy.stats import multivariate_normal
from sklearn.covariance import EmpiricalCovariance
from robustbench.utils import load_model
import torch.nn.functional as F
from torch.utils.data import TensorDataset
from torch.optim.lr_scheduler import StepLR
from tqdm.notebook import tqdm


def compute_fpr95(labels, scores):

    fpr, tpr, thresholds = roc_curve(labels, scores)
    idx = np.abs(tpr - 0.95).argmin()  # Find the index where TPR is closest to 0.95
    fpr95 = fpr[idx]

    return fpr95

def compute_auroc(labels, scores):

    return roc_auc_score(labels, scores)

def compute_aupr(labels, scores):

    precision, recall, _ = precision_recall_curve(labels, scores)
    return auc(recall, precision)


# Compute metrics


def get_clean_AUC(model, test_loader , device, num_classes):
    is_train = model.training
    model.eval()

    soft = torch.nn.Softmax(dim=1)
    anomaly_scores = []
    preds = []
    test_labels = []
    test_labels_acc = []

    with torch.no_grad():
        with tqdm(test_loader, unit="batch") as tepoch:
            torch.cuda.empty_cache()
            for i, (data, target) in enumerate(tepoch):
                data, target = data.to(device), target.to(device)
                output = model(data)

                predictions = output.argmax(dim=1, keepdim=True).squeeze()
                preds += predictions.detach().cpu().numpy().tolist()

                # probs = soft(output).squeeze()
                # anomaly_scores += probs[:, num_classes].detach().cpu().numpy().tolist() model

                probs = soft(output)
                max_probabilities,_ = torch.max(probs[:,:num_classes] , dim=1)
                anomaly_scores+=max_probabilities.detach().cpu().numpy().tolist()
                # anomaly_scores += probs[:, num_classes].detach().cpu().numpy().tolist()
                target = target == num_classes

                test_labels += target.detach().cpu().numpy().tolist()
    anomaly_scores=[x * -1 for x in anomaly_scores]
    auc = roc_auc_score(test_labels,  anomaly_scores)
 
    fpr95 = compute_fpr95(test_labels, anomaly_scores)
    auroc = compute_auroc(test_labels, anomaly_scores)
    aupr = compute_aupr(test_labels, anomaly_scores)

    print(f"FPR95: {fpr95}")
    print(f"AUROC is: {auroc}")
    print(f"AUPR: {aupr}")

    return auc



def wrapper_method(func):
    def wrapper_func(self, *args, **kwargs):
        result = func(self, *args, **kwargs)
        for atk in self.__dict__.get('_attacks').values():
            eval("atk."+func.__name__+"(*args, **kwargs)")
        return result
    return wrapper_func


class Attack(object):
    r"""
    Base class for all attacks.

    .. note::
        It automatically set device to the device where given model is.
        It basically changes training mode to eval during attack process.
        To change this, please see `set_model_training_mode`.
    """

    def __init__(self, name, model):
        r"""
        Initializes internal attack state.

        Arguments:
            name (str): name of attack.
            model (torch.nn.Module): model to attack.
        """

        self.attack = name
        self._attacks = OrderedDict()

        self.set_model(model)
        self.device = next(model.parameters()).device

        # Controls attack mode.
        self.attack_mode = 'default'
        self.supported_mode = ['default']
        self.targeted = False
        self._target_map_function = None

        # Controls when normalization is used.
        self.normalization_used = {}
        self._normalization_applied = False
        self._set_auto_normalization_used(model)

        # Controls model mode during attack.
        self._model_training = False
        self._batchnorm_training = False
        self._dropout_training = False

    def forward(self, inputs, labels=None, *args, **kwargs):
        r"""
        It defines the computation performed at every call.
        Should be overridden by all subclasses.
        """
        raise NotImplementedError

    def _check_inputs(self, images):
        tol = 1e-4
        if self._normalization_applied:
            images = self.inverse_normalize(images)
        if torch.max(images) > 1+tol or torch.min(images) < 0-tol:
            raise ValueError('Input must have a range [0, 1] (max: {}, min: {})'.format(
                torch.max(images), torch.min(images)))
        return images

    def _check_outputs(self, images):
        if self._normalization_applied:
            images = self.normalize(images)
        return images

    @wrapper_method
    def set_model(self, model):
        self.model = model
        self.model_name = model.__class__.__name__

    def get_logits(self, inputs, labels=None, *args, **kwargs):
        if self._normalization_applied:
            inputs = self.normalize(inputs)
        logits = self.model(inputs)
        return logits

    @wrapper_method
    def _set_normalization_applied(self, flag):
        self._normalization_applied = flag

    @wrapper_method
    def set_device(self, device):
        self.device = device

    @wrapper_method
    def _set_auto_normalization_used(self, model):
        if model.__class__.__name__ == 'RobModel':
            mean = getattr(model, 'mean', None)
            std = getattr(model, 'std', None)
            if (mean is not None) and (std is not None):
                if isinstance(mean, torch.Tensor):
                    mean = mean.cpu().numpy()
                if isinstance(std, torch.Tensor):
                    std = std.cpu().numpy()
                if (mean != 0).all() or (std != 1).all():
                    self.set_normalization_used(mean, std)
    #                 logging.info("Normalization automatically loaded from `model.mean` and `model.std`.")

    @wrapper_method
    def set_normalization_used(self, mean, std):
        n_channels = len(mean)
        mean = torch.tensor(mean).reshape(1, n_channels, 1, 1)
        std = torch.tensor(std).reshape(1, n_channels, 1, 1)
        self.normalization_used['mean'] = mean
        self.normalization_used['std'] = std
        self._normalization_applied = True

    def normalize(self, inputs):
        mean = self.normalization_used['mean'].to(inputs.device)
        std = self.normalization_used['std'].to(inputs.device)
        return (inputs - mean) / std

    def inverse_normalize(self, inputs):
        mean = self.normalization_used['mean'].to(inputs.device)
        std = self.normalization_used['std'].to(inputs.device)
        return inputs*std + mean

    def get_mode(self):
        r"""
        Get attack mode.

        """
        return self.attack_mode

    @wrapper_method
    def set_mode_default(self):
        r"""
        Set attack mode as default mode.

        """
        self.attack_mode = 'default'
        self.targeted = False
        print("Attack mode is changed to 'default.'")

    @wrapper_method
    def _set_mode_targeted(self, mode, quiet):
        if "targeted" not in self.supported_mode:
            raise ValueError("Targeted mode is not supported.")
        self.targeted = True
        self.attack_mode = mode
        if not quiet:
            print("Attack mode is changed to '%s'." % mode)

    @wrapper_method
    def set_mode_targeted_by_function(self, target_map_function, quiet=False):
        r"""
        Set attack mode as targeted.

        Arguments:
            target_map_function (function): Label mapping function.
                e.g. lambda inputs, labels:(labels+1)%10.
                None for using input labels as targeted labels. (Default)

        """
        self._set_mode_targeted('targeted(custom)', quiet)
        self._target_map_function = target_map_function

    @wrapper_method
    def set_mode_targeted_random(self, quiet=False):
        r"""
        Set attack mode as targeted with random labels.

        Arguments:
            num_classses (str): number of classes.

        """
        self._set_mode_targeted('targeted(random)', quiet)
        self._target_map_function = self.get_random_target_label

    @wrapper_method
    def set_mode_targeted_least_likely(self, kth_min=1, quiet=False):
        r"""
        Set attack mode as targeted with least likely labels.

        Arguments:
            kth_min (str): label with the k-th smallest probability used as target labels. (Default: 1)

        """
        self._set_mode_targeted('targeted(least-likely)', quiet)
        assert (kth_min > 0)
        self._kth_min = kth_min
        self._target_map_function = self.get_least_likely_label

    @wrapper_method
    def set_mode_targeted_by_label(self, quiet=False):
        r"""
        Set attack mode as targeted.

        .. note::
            Use user-supplied labels as target labels.
        """
        self._set_mode_targeted('targeted(label)', quiet)
        self._target_map_function = 'function is a string'

    @wrapper_method
    def set_model_training_mode(self, model_training=False, batchnorm_training=False, dropout_training=False):
        r"""
        Set training mode during attack process.

        Arguments:
            model_training (bool): True for using training mode for the entire model during attack process.
            batchnorm_training (bool): True for using training mode for batchnorms during attack process.
            dropout_training (bool): True for using training mode for dropouts during attack process.

        .. note::
            For RNN-based models, we cannot calculate gradients with eval mode.
            Thus, it should be changed to the training mode during the attack.
        """
        self._model_training = model_training
        self._batchnorm_training = batchnorm_training
        self._dropout_training = dropout_training

    @wrapper_method
    def _change_model_mode(self, given_training):
        if self._model_training:
            self.model.train()
            for _, m in self.model.named_modules():
                if not self._batchnorm_training:
                    if 'BatchNorm' in m.__class__.__name__:
                        m = m.eval()
                if not self._dropout_training:
                    if 'Dropout' in m.__class__.__name__:
                        m = m.eval()
        else:
            self.model.eval()

    @wrapper_method
    def _recover_model_mode(self, given_training):
        if given_training:
            self.model.train()

    def save(self, data_loader, save_path=None, verbose=True, return_verbose=False,
             save_predictions=False, save_clean_inputs=False, save_type='float'):
        r"""
        Save adversarial inputs as torch.tensor from given torch.utils.data.DataLoader.

        Arguments:
            save_path (str): save_path.
            data_loader (torch.utils.data.DataLoader): data loader.
            verbose (bool): True for displaying detailed information. (Default: True)
            return_verbose (bool): True for returning detailed information. (Default: False)
            save_predictions (bool): True for saving predicted labels (Default: False)
            save_clean_inputs (bool): True for saving clean inputs (Default: False)

        """
        if save_path is not None:
            adv_input_list = []
            label_list = []
            if save_predictions:
                pred_list = []
            if save_clean_inputs:
                input_list = []

        correct = 0
        total = 0
        l2_distance = []

        total_batch = len(data_loader)
        given_training = self.model.training

        for step, (inputs, labels) in enumerate(data_loader):
            start = time.time()
            adv_inputs = self.__call__(inputs, labels)
            batch_size = len(inputs)

            if verbose or return_verbose:
                with torch.no_grad():
                    outputs = self.get_output_with_eval_nograd(adv_inputs)

                    # Calculate robust accuracy
                    _, pred = torch.max(outputs.data, 1)
                    total += labels.size(0)
                    right_idx = (pred == labels.to(self.device))
                    correct += right_idx.sum()
                    rob_acc = 100 * float(correct) / total

                    # Calculate l2 distance
                    delta = (adv_inputs - inputs.to(self.device)).view(batch_size, -1)
                    l2_distance.append(torch.norm(delta[~right_idx], p=2, dim=1))
                    l2 = torch.cat(l2_distance).mean().item()

                    # Calculate time computation
                    progress = (step+1)/total_batch*100
                    end = time.time()
                    elapsed_time = end-start

                    if verbose:
                        self._save_print(progress, rob_acc, l2, elapsed_time, end='\r')

            if save_path is not None:
                adv_input_list.append(adv_inputs.detach().cpu())
                label_list.append(labels.detach().cpu())

                adv_input_list_cat = torch.cat(adv_input_list, 0)
                label_list_cat = torch.cat(label_list, 0)

                save_dict = {'adv_inputs': adv_input_list_cat, 'labels': label_list_cat}

                if save_predictions:
                    pred_list.append(pred.detach().cpu())
                    pred_list_cat = torch.cat(pred_list, 0)
                    save_dict['preds'] = pred_list_cat

                if save_clean_inputs:
                    input_list.append(inputs.detach().cpu())
                    input_list_cat = torch.cat(input_list, 0)
                    save_dict['clean_inputs'] = input_list_cat

                if self.normalization_used is not None:
                    save_dict['adv_inputs'] = self.inverse_normalize(save_dict['adv_inputs'])
                    if save_clean_inputs:
                        save_dict['clean_inputs'] = self.inverse_normalize(save_dict['clean_inputs'])

                if save_type == 'int':
                    save_dict['adv_inputs'] = self.to_type(save_dict['adv_inputs'], 'int')
                    if save_clean_inputs:
                        save_dict['clean_inputs'] = self.to_type(save_dict['clean_inputs'], 'int')

                save_dict['save_type'] = save_type
                torch.save(save_dict, save_path)

        # To avoid erasing the printed information.
        if verbose:
            self._save_print(progress, rob_acc, l2, elapsed_time, end='\n')

        if given_training:
            self.model.train()

        if return_verbose:
            return rob_acc, l2, elapsed_time

    @staticmethod
    def to_type(inputs, type):
        r"""
        Return inputs as int if float is given.
        """
        if type == 'int':
            if isinstance(inputs, torch.FloatTensor) or isinstance(inputs, torch.cuda.FloatTensor):
                return (inputs*255).type(torch.uint8)
        elif type == 'float':
            if isinstance(inputs, torch.ByteTensor) or isinstance(inputs, torch.cuda.ByteTensor):
                return inputs.float()/255
        else:
            raise ValueError(
                type + " is not a valid type. [Options: float, int]")
        return inputs

    @staticmethod
    def _save_print(progress, rob_acc, l2, elapsed_time, end):
        print('- Save progress: %2.2f %% / Robust accuracy: %2.2f %% / L2: %1.5f (%2.3f it/s) \t'
              % (progress, rob_acc, l2, elapsed_time), end=end)

    @staticmethod
    def load(load_path, batch_size=128, shuffle=False, normalize=None,
             load_predictions=False, load_clean_inputs=False):
        save_dict = torch.load(load_path)
        keys = ['adv_inputs', 'labels']

        if load_predictions:
            keys.append('preds')
        if load_clean_inputs:
            keys.append('clean_inputs')

        if save_dict['save_type'] == 'int':
            save_dict['adv_inputs'] = save_dict['adv_inputs'].float()/255
            if load_clean_inputs:
                save_dict['clean_inputs'] = save_dict['clean_inputs'].float() / 255  # nopep8

        if normalize is not None:
            n_channels = len(normalize['mean'])
            mean = torch.tensor(normalize['mean']).reshape(1, n_channels, 1, 1)
            std = torch.tensor(normalize['std']).reshape(1, n_channels, 1, 1)
            save_dict['adv_inputs'] = (save_dict['adv_inputs'] - mean) / std
            if load_clean_inputs:
                save_dict['clean_inputs'] = (save_dict['clean_inputs'] - mean) / std  # nopep8

        adv_data = TensorDataset(*[save_dict[key] for key in keys])
        adv_loader = DataLoader(
            adv_data, batch_size=batch_size, shuffle=shuffle)
        print("Data is loaded in the following order: [%s]" % (", ".join(keys)))  # nopep8
        return adv_loader

    @torch.no_grad()
    def get_output_with_eval_nograd(self, inputs):
        given_training = self.model.training
        if given_training:
            self.model.eval()
        outputs = self.get_logits(inputs)
        if given_training:
            self.model.train()
        return outputs

    def get_target_label(self, inputs, labels=None):
        r"""
        Function for changing the attack mode.
        Return input labels.
        """
        if self._target_map_function is None:
            raise ValueError(
                'target_map_function is not initialized by set_mode_targeted.')
        if self.attack_mode == 'targeted(label)':
            target_labels = labels
        else:
            target_labels = self._target_map_function(inputs, labels)
        return target_labels

    @torch.no_grad()
    def get_least_likely_label(self, inputs, labels=None):
        outputs = self.get_output_with_eval_nograd(inputs)
        if labels is None:
            _, labels = torch.max(outputs, dim=1)
        n_classses = outputs.shape[-1]

        target_labels = torch.zeros_like(labels)
        for counter in range(labels.shape[0]):
            l = list(range(n_classses))
            l.remove(labels[counter])
            _, t = torch.kthvalue(outputs[counter][l], self._kth_min)
            target_labels[counter] = l[t]

        return target_labels.long().to(self.device)

    @torch.no_grad()
    def get_random_target_label(self, inputs, labels=None):
        outputs = self.get_output_with_eval_nograd(inputs)
        if labels is None:
            _, labels = torch.max(outputs, dim=1)
        n_classses = outputs.shape[-1]

        target_labels = torch.zeros_like(labels)
        for counter in range(labels.shape[0]):
            l = list(range(n_classses))
            l.remove(labels[counter])
            t = (len(l)*torch.rand([1])).long().to(self.device)
            target_labels[counter] = l[t]

        return target_labels.long().to(self.device)

    def __call__(self, images, labels=None, *args, **kwargs):
        given_training = self.model.training
        self._change_model_mode(given_training)
        images = self._check_inputs(images)
        adv_images = self.forward(images, labels, *args, **kwargs)
        adv_images = self._check_outputs(adv_images)
        self._recover_model_mode(given_training)
        return adv_images

    def __repr__(self):
        info = self.__dict__.copy()

        del_keys = ['model', 'attack', 'supported_mode']

        for key in info.keys():
            if key[0] == "_":
                del_keys.append(key)

        for key in del_keys:
            del info[key]

        info['attack_mode'] = self.attack_mode
        info['normalization_used'] = True if len(self.normalization_used) > 0 else False  # nopep8

        return self.attack + "(" + ', '.join('{}={}'.format(key, val) for key, val in info.items()) + ")"

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)

        attacks = self.__dict__.get('_attacks')

        # Get all items in iterable items.
        def get_all_values(items, stack=[]):
            if (items not in stack):
                stack.append(items)
                if isinstance(items, list) or isinstance(items, dict):
                    if isinstance(items, dict):
                        items = (list(items.keys())+list(items.values()))
                    for item in items:
                        yield from get_all_values(item, stack)
                else:
                    if isinstance(items, Attack):
                        yield items
            else:
                if isinstance(items, Attack):
                    yield items

        for num, value in enumerate(get_all_values(value)):
            attacks[name+"."+str(num)] = value
            for subname, subvalue in value.__dict__.get('_attacks').items():
                attacks[name+"."+subname] = subvalue






def get_auc_adversarial(model, test_loader, test_attack,   device, num_classes):
    is_train = model.training
    model.eval()

    soft = torch.nn.Softmax(dim=1)
    anomaly_scores = []
    preds = []
    test_labels = []
    test_labels_acc = []
    with tqdm(test_loader, unit="batch") as tepoch:
            torch.cuda.empty_cache()
            for i, (data, target) in enumerate(tepoch):
                data, target = data.to(device), target.to(device)
                adv_data = test_attack(data, target)
                output  = model(adv_data)
                predictions = output.argmax(dim=1, keepdim=True).squeeze()
                preds += predictions.detach().cpu().numpy().tolist()
                probs = soft(output)
                max_probabilities,_ = torch.max(probs[:,:num_classes] , dim=1)
                anomaly_scores+=max_probabilities.detach().cpu().numpy().tolist()
                target = target == num_classes

                test_labels += target.detach().cpu().numpy().tolist()
    anomaly_scores=[x * -1 for x in anomaly_scores]
    auc = roc_auc_score(test_labels,  anomaly_scores)
    fpr95 = compute_fpr95(test_labels, anomaly_scores)
    auroc = compute_auroc(test_labels, anomaly_scores)
    aupr = compute_aupr(test_labels, anomaly_scores)

    print(f"FPR95: {fpr95}")
    print(f"AUROC is: {auroc}")
    print(f"AUPR: {aupr}")


    if is_train:
        model.train()
    else:
        model.eval()

    return auc






class PGD_AUC(Attack):
    r"""
    PGD in the paper 'Towards Deep Learning Models Resistant to Adversarial Attacks'
    [https://arxiv.org/abs/1706.06083]

    Distance Measure : Linf

    Arguments:
        model (nn.Module): model to attack.
        eps (float): maximum perturbation. (Default: 8/255)
        alpha (float): step size. (Default: 2/255)
        steps (int): number of steps. (Default: 10)
        random_start (bool): using random initialization of delta. (Default: True)

    Shape:
        - images: :math:`(N, C, H, W)` where `N = number of batches`, `C = number of channels`,        `H = height` and `W = width`. It must have a range [0, 1].
        - labels: :math:`(N)` where each value :math:`y_i` is :math:`0 \leq y_i \leq` `number of labels`.
        - output: :math:`(N, C, H, W)`.

    Examples::
        >>> attack = torchattacks.PGD(model, eps=8/255, alpha=1/255, steps=10, random_start=True)
        >>> adv_images = attack(images, labels)

    """

    def __init__(self, model, eps=8/255, alpha=2/255, steps=10, random_start=True, num_classes=10):
        super().__init__("PGD", model)
        self.eps = eps
        self.alpha = alpha
        self.steps = steps
        self.random_start = random_start
        self.supported_mode = ['default', 'targeted']
        self.num_classes = num_classes

    def forward(self, images, labels):
        r"""
        Overridden.
        """

        images = images.clone().detach().to(self.device)
        labels = labels.clone().detach().to(self.device)

        softmax = nn.Softmax(dim=1)
        adv_images = images.clone().detach()

        if self.random_start:
            # Starting at a uniformly random point
            adv_images = adv_images + \
                torch.empty_like(adv_images).uniform_(-self.eps, self.eps)
            adv_images = torch.clamp(adv_images, min=0, max=1).detach()

        ones = torch.ones_like(labels)
        multipliers = -1*(ones - 2 * ones * (labels == self.num_classes))

        for _ in range(self.steps):
            adv_images.requires_grad = True
            outputs = self.get_logits(adv_images)
            final_outputs = softmax(outputs)
            # choose the value of probability of ood

            max_probabilities  = torch.max(final_outputs  , dim=1)[0]



            cost = torch.sum(max_probabilities * multipliers)



            # Update adversarial images
            grad = torch.autograd.grad(cost, adv_images,
                                       retain_graph=False, create_graph=False)[0]

            adv_images = adv_images.detach() + self.alpha*grad.sign()
            delta = torch.clamp(adv_images - images,
                                min=-self.eps, max=self.eps)
            adv_images = torch.clamp(images + delta, min=0, max=1).detach()
        return adv_images






def auc_MSP_adversarial(model, test_loader, test_attack,   device, num_classes):
    is_train = model.training
    model.eval()

    soft = torch.nn.Softmax(dim=1)
    anomaly_scores = []
    preds = []
    test_labels = []
    test_labels_acc = []
    with tqdm(test_loader, unit="batch") as tepoch:
            torch.cuda.empty_cache()
            for i, (data, target) in enumerate(tepoch):
                data, target = data.to(device), target.to(device)
                adv_data = test_attack(data, target)

                output  = model(adv_data)

                predictions = output.argmax(dim=1, keepdim=True).squeeze()
                preds += predictions.detach().cpu().numpy().tolist()

                # probs = soft(output).squeeze()
                # anomaly_scores += probs[:, num_classes].detach().cpu().numpy().tolist()


                probs = soft(output)

                max_probabilities,_ = torch.max(probs[:,:num_classes] , dim=1)




                anomaly_scores+=max_probabilities.detach().cpu().numpy().tolist()
                # anomaly_scores += probs[:, num_classes].detach().cpu().numpy().tolist()
                target = target == num_classes
                test_labels += target.detach().cpu().numpy().tolist()
    anomaly_scores=[x * -1 for x in anomaly_scores]
    auc = roc_auc_score(test_labels,  anomaly_scores)

    if is_train:
        model.train()
    else:
        model.eval()

    print(auc)
    return auc


class PGD_MSP(Attack):
    r"""
    PGD in the paper 'Towards Deep Learning Models Resistant to Adversarial Attacks'
    [https://arxiv.org/abs/1706.06083]

    Distance Measure : Linf

    Arguments:
        model (nn.Module): model to attack.
        eps (float): maximum perturbation. (Default: 8/255)
        alpha (float): step size. (Default: 2/255)
        steps (int): number of steps. (Default: 10)
        random_start (bool): using random initialization of delta. (Default: True)

    Shape:
        - images: :math:`(N, C, H, W)` where `N = number of batches`, `C = number of channels`,        `H = height` and `W = width`. It must have a range [0, 1].
        - labels: :math:`(N)` where each value :math:`y_i` is :math:`0 \leq y_i \leq` `number of labels`.
        - output: :math:`(N, C, H, W)`.

    Examples::
        >>> attack = torchattacks.PGD(model, eps=8/255, alpha=1/255, steps=10, random_start=True)
        >>> adv_images = attack(images, labels)

    """

    def __init__(self, model, eps=8/255, alpha=2/255, steps=10, random_start=True, num_classes=10):
        super().__init__("PGD", model)
        self.eps = eps
        self.alpha = alpha
        self.steps = steps
        self.random_start = random_start
        self.supported_mode = ['default', 'targeted']
        self.num_classes = num_classes

    def forward(self, images, labels):
        r"""
        Overridden.
        """

        images = images.clone().detach().to(self.device)
        labels = labels.clone().detach().to(self.device)

        softmax = nn.Softmax(dim=1)
        adv_images = images.clone().detach()

        if self.random_start:
            # Starting at a uniformly random point
            adv_images = adv_images + \
                torch.empty_like(adv_images).uniform_(-self.eps, self.eps)
            adv_images = torch.clamp(adv_images, min=0, max=1).detach()

        ones = torch.ones_like(labels)
        multipliers = -1*(ones - 2 * ones * (labels == self.num_classes))

        for _ in range(self.steps):
            adv_images.requires_grad = True
            outputs = self.get_logits(adv_images)
            final_outputs = softmax(outputs)
            # choose the value of probability of ood

            max_probabilities  = torch.max(final_outputs  , dim=1)[0]



            cost = torch.sum(max_probabilities * multipliers)



            # Update adversarial images
            grad = torch.autograd.grad(cost, adv_images,
                                       retain_graph=False, create_graph=False)[0]

            adv_images = adv_images.detach() + self.alpha*grad.sign()
            delta = torch.clamp(adv_images - images,
                                min=-self.eps, max=self.eps)
            adv_images = torch.clamp(images + delta, min=0, max=1).detach()

        # imshow(torchvision.utils.make_grid(adv_images.cpu()))
        # print(multipliers)
        return adv_images





def auc_MSP(model, test_loader , device, num_classes):
    is_train = model.training
    model.eval()

    soft = torch.nn.Softmax(dim=1)
    anomaly_scores = []
    preds = []
    test_labels = []
    test_labels_acc = []

    with torch.no_grad():
        with tqdm(test_loader, unit="batch") as tepoch:
            torch.cuda.empty_cache()
            for i, (data, target) in enumerate(tepoch):
                data, target = data.to(device), target.to(device)
                output = model(data)

                predictions = output.argmax(dim=1, keepdim=True).squeeze()
                preds += predictions.detach().cpu().numpy().tolist()

                # probs = soft(output).squeeze()
                # anomaly_scores += probs[:, num_classes].detach().cpu().numpy().tolist() model

                probs = soft(output)
                max_probabilities,_ = torch.max(probs[:,:num_classes] , dim=1)
                anomaly_scores+=max_probabilities.detach().cpu().numpy().tolist()
                # anomaly_scores += probs[:, num_classes].detach().cpu().numpy().tolist()
                target = target == num_classes

                test_labels += target.detach().cpu().numpy().tolist()
    anomaly_scores=[x * -1 for x in anomaly_scores]
    auc = roc_auc_score(test_labels,  anomaly_scores)
    print(auc)


    return auc

