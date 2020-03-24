import cv2

from ..dataset.transformer import TransformerCV
from .embedder import *
from .extractor import *

import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from .loss import scale_rotate_offset_dist, sample_semi_hard_feature, clamp_loss_all
from .operator import normalize_coordinates

import os

from ..train.train_tools import to_cuda, dim_extend

name2embedder={
    "BilinearGCNN":BilinearGCNN,
    "BilinearRotationGCNN":BilinearRotationGCNN,
    "GRENone":GRENone,
    "None": lambda cfg: None,
}
name2extractor={
    "VanillaLightCNN": VanillaLightCNN,
    "None": lambda cfg: None,
}

def interpolate_feats(img,pts,feats):
    # compute location on the feature map (due to pooling)
    _, _, h, w = feats.shape
    pool_num = img.shape[-1] // feats.shape[-1]
    pts_warp=(pts+0.5)/pool_num-0.5
    pts_norm=normalize_coordinates(pts_warp,h,w)
    pts_norm=torch.unsqueeze(pts_norm, 1)  # b,1,n,2

    # interpolation
    pfeats=F.grid_sample(feats, pts_norm, 'bilinear')[:, :, 0, :]  # b,f,n
    pfeats=pfeats.permute(0,2,1) # b,n,f
    return pfeats

class ExtractorWrapper(nn.Module):
    def __init__(self,cfg):
        super().__init__()
        self.extractor=name2extractor[cfg['extractor']](cfg)
        self.sn, self.rn = cfg['sample_scale_num'], cfg['sample_rotate_num']

    def forward(self,img_list,pts_list,grid_list=None):
        '''

        :param img_list:  list of [b,3,h,w]
        :param pts_list:  list of [b,n,2]
        :param grid_list:  list of [b,hn,wn,2]
        :return:gefeats [b,n,f,sn,rn]
        '''
        assert(len(img_list)==self.rn*self.sn)
        gfeats_list,neg_gfeats_list=[],[]
        # feature extraction
        for img_index,img in enumerate(img_list):
            # extract feature
            feats=self.extractor(img)
            gfeats_list.append(interpolate_feats(img,pts_list[img_index],feats)[:,:,:,None])
            if grid_list is not None:
                _,hn,wn,_=grid_list[img_index].shape
                grid_pts=grid_list[img_index].reshape(-1,hn*wn,2)
                neg_gfeats_list.append(interpolate_feats(img,grid_pts,feats)[:,:,:,None])

        gfeats_list=torch.cat(gfeats_list,3)  # b,n,f,sn*rn
        b,n,f,_=gfeats_list.shape
        gfeats_list=gfeats_list.reshape(b,n,f,self.sn,self.rn)
        if grid_list is not None:
            neg_gfeats_list = torch.cat(neg_gfeats_list, 3) # b,hn*wn,f,sn*rn
            b,hn,wn,_=grid_list[0].shape
            b,_,f,srn=neg_gfeats_list.shape
            neg_gfeats_list=neg_gfeats_list.reshape(b,hn,wn,f,self.sn,self.rn) # b,hn,wn,f,sn*rn
            return gfeats_list, neg_gfeats_list
        else:
            return gfeats_list

class EmbedderWrapper(nn.Module):
    def __init__(self,cfg):
        super().__init__()
        self.embedder=name2embedder[cfg['embedder']](cfg)
        self.sn, self.rn = cfg['sample_scale_num'], cfg['sample_rotate_num']

    def forward(self, gfeats):
        # group cnns
        b,n,f,sn,rn=gfeats.shape
        assert(sn==self.sn and rn==self.rn)
        gefeats=self.embedder(gfeats) # b,n,f
        return gefeats

class TrainWrapper(nn.Module):
    def __init__(self,cfg):
        super().__init__()
        self.extractor_wrapper=ExtractorWrapper(cfg)
        self.embedder_wrapper=EmbedderWrapper(cfg)
        self.config=cfg
        self.sn, self.rn = cfg['sample_scale_num'], cfg['sample_rotate_num']
        self.loss_margin = cfg['loss_margin']
        self.hem_interval = cfg['hem_interval']
        self.train_embedder = cfg['train_embedder']
        self.train_extractor = cfg['train_extractor']

    def forward(self, img_list0, pts_list0, pts0, grid_list0, img_list1, pts_list1, pts1, grid_list1, scale_offset, rotate_offset, hem_thresh, loss_type='gfeats'):
        '''
        :param img_list0:   [sn,rn,b,3,h,w]
        :param pts_list0:   [sn,rn,b,n,2]
        :param pts0:        [b,n,2]
        :param grid_list0:  [sn,rn,b,hn,wn,2]
        :param img_list1:   [sn,rn,b,3,h,w]
        :param pts_list1:   [sn,rn,b,n,2]
        :param pts1:        [b,n,2]
        :param grid_list1:  [sn,rn,b,hn,wn,2]
        :param scale_offset:  [b,n]
        :param rotate_offset: [b,n]
        :param hem_thresh:
        :param loss_type: 'gfeats' or 'gefeats'
        :return:
        '''
        gfeats0 = self.extractor_wrapper(img_list0,pts_list0)  # [b,n,fg,sn,rn]
        if self.train_extractor:
            gfeats1, gfeats_neg = self.extractor_wrapper(img_list1,pts_list1,grid_list1) # [b,n,fg,sn,rn] [b,hn,wn,fg,sn,rn]
        else:
            with torch.no_grad():
                gfeats1, gfeats_neg = self.extractor_wrapper(img_list1, pts_list1, grid_list1)  # [b,n,fg,sn,rn] [b,hn,wn,fg,sn,rn]

        b, hn, wn, fg, sn, rn = gfeats_neg.shape
        b, n, fg, sn, rn = gfeats0.shape
        assert(sn==self.sn and rn==self.rn)
        pts_shem_gt=pts1/self.hem_interval
        hem_thresh=hem_thresh/self.hem_interval

        if loss_type=='gfeats':
            # pos distance [b,n]
            dis_pos = scale_rotate_offset_dist(gfeats0.permute(0, 1, 3, 4, 2), gfeats1.permute(0, 1, 3, 4, 2), scale_offset, rotate_offset, self.sn, self.rn)
            dis_pos=dis_pos[:,None,None,:].repeat(1, sn, rn,1).reshape(b*sn*rn,n) # b*sn*rn,n

            # neg search
            gfeats_neg=gfeats_neg.permute(0,4,5,3,1,2)
            gfeats_neg=gfeats_neg.reshape(b*sn*rn,fg,hn,wn)

            pts_shem_gt = pts_shem_gt[:, None, None, :, :].repeat(1, sn, rn, 1, 1).reshape(b * sn * rn, n, 2)  # b*sn*rn,n

            gfeats0=gfeats0.permute(0, 3, 4, 1, 2)
            gfeats0=gfeats0.reshape(b * sn * rn, n, fg)
            if self.config['loss_square']:
                dis_pos = dis_pos * dis_pos
                gfeats_shem_neg = sample_semi_hard_feature(gfeats_neg, dis_pos, gfeats0, pts_shem_gt, 1, hem_thresh, self.loss_margin, True)
                dis_neg = torch.norm(gfeats0-gfeats_shem_neg, 2, 2) # b*sn*rn,n
                dis_neg = dis_neg * dis_neg
            else:
                gfeats_shem_neg = sample_semi_hard_feature(gfeats_neg, dis_pos, gfeats0, pts_shem_gt, 1, hem_thresh, self.loss_margin) # b*sn*rn,n,fg
                # neg distance [b,n]
                dis_neg = torch.norm(gfeats0-gfeats_shem_neg, 2, 2) # b*sn*rn,n
        else:
            assert(loss_type=='gefeats')
            if self.train_embedder or self.config['embedder']=='GRENone':
                efeats0=self.embedder_wrapper(gfeats0)     # b,n,fe
                efeats1=self.embedder_wrapper(gfeats1)     # b,n,fe
                efeats_neg=self.embedder_wrapper(gfeats_neg.reshape(b,hn*wn,fg,sn,rn)) # b,hn*wn,fe
            else:
                with torch.no_grad():
                    efeats0=self.embedder_wrapper(gfeats0)     # b,n,fe
                    efeats1=self.embedder_wrapper(gfeats1)     # b,n,fe
                    efeats_neg=self.embedder_wrapper(gfeats_neg.reshape(b,hn*wn,fg,sn,rn)) # b,hn*wn,fe

            # pos distance
            dis_pos=torch.norm(efeats0-efeats1, 2, 2)

            # neg search
            fe=efeats_neg.shape[-1]
            efeats_neg=efeats_neg.reshape(b,hn,wn,fe).permute(0,3,1,2)
            if self.config['loss_square']:
                dis_pos = dis_pos * dis_pos
                efeats_shem_neg = sample_semi_hard_feature(efeats_neg, dis_pos, efeats0, pts_shem_gt, 1, hem_thresh, self.loss_margin, True)
                dis_neg = torch.norm(efeats0-efeats_shem_neg, 2, 2)
                dis_neg = dis_neg * dis_neg
            else:
                efeats_shem_neg = sample_semi_hard_feature(efeats_neg, dis_pos, efeats0, pts_shem_gt, 1, hem_thresh, self.loss_margin)
                dis_neg = torch.norm(efeats0-efeats_shem_neg, 2, 2)

        triplet_loss, triplet_neg_rate = clamp_loss_all(dis_pos - dis_neg + self.loss_margin)

        results = {
            'triplet_loss': triplet_loss,
            'triplet_neg_rate': triplet_neg_rate,
            'dis_pos': dis_pos,
            'dis_neg': dis_neg,
        }
        return results

class GIFTDescriptor:
    def __init__(self,cfg):
        self.extractor=ExtractorWrapper(cfg).cuda()
        self.embedder=EmbedderWrapper(cfg).cuda()
        self._load_model(cfg['model_dir'],cfg['step'])
        self.transformer = TransformerCV(cfg)

    def __call__(self, img, pts):
        h,w=img.shape[:2]
        transformed_imgs=self.transformer.transform(img,pts)
        with torch.no_grad():
            img_list,pts_list=to_cuda(self.transformer.postprocess_transformed_imgs(transformed_imgs))
            gfeats=self.extractor(dim_extend(img_list),dim_extend(pts_list))
            efeats=self.embedder(gfeats)[0].detach().cpu().numpy()
        return efeats

    def _load_model(self, model_dir, step=-1):
        pths = [int(pth.split('.')[0]) for pth in os.listdir(model_dir)]
        if len(pths) == 0:
            return 0
        if step == -1:
            pth = max(pths)
        else:
            pth = step

        pretrained_model = torch.load(os.path.join(model_dir, '{}.pth'.format(pth)))
        self.extractor.load_state_dict(pretrained_model['extractor'])
        self.embedder.load_state_dict(pretrained_model['embedder'])
        print('load {} step {}'.format(model_dir, pretrained_model['step']))
        self.step = pretrained_model['step'] + 1
