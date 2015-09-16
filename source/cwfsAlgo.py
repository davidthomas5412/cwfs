#!/usr/bin/env python
##
## @package cwfs
## @file cwfsAlgo.py
##
## @authors: Bo Xin & Chuck Claver
## @       Large Synoptic Survey Telescope
##
## The FFT algorithm in solvePoissonEq() is partly based on some earlier code written by William P. Kuhn
##

import os, sys
import string
import numpy as np
import scipy.ndimage as ndimage
import copy

import matplotlib.pyplot as plt

from cwfsTools import padArray
from cwfsTools import extractArray
from cwfsTools import ZernikeMaskedFit
from cwfsTools import ZernikeAnnularEval
from cwfsTools import ZernikeAnnularGrad
from cwfsTools import ZernikeEval
from cwfsTools import ZernikeGrad

class cwfsAlgo(object):

    def __init__(self, algoFile,inst,debug_level):
        self.filename=os.path.join('data/algo/',(algoFile+'.algo'))
        fid=open(self.filename)

        iscomment = False
        for line in fid:
            line = line.strip()
            if (line.startswith('###')):
                iscomment = ~iscomment
            if ((line.startswith('#') == False) and (iscomment == False) and len(line) >0):
                if (line.startswith('PoissonSolver')):
                    self.PoissonSolver = line.split()[1]
                if (line.startswith('Num_of_Zernikes')):
                    self.numTerms = string.atoi(line.split()[1])
                if (line.startswith('ZTerms')):
                    self.ZTerms = map(int,line.split()[1:])
                if (line.startswith('Num_of_outer_itr')):
                    self.outerItr = string.atoi(line.split()[1])
                if (line.startswith('Num_of_inner_itr')):
                    self.innerItr = string.atoi(line.split()[1])
                if (line.startswith('Zernikes')):
                    self.zobsR = string.atoi(line.split()[1])
                if (line.startswith('Increase_resolution')):
                    self.upReso = string.atof(line.split()[1])
                if (line.startswith('FFT_dimension')):
                    self.padDim = string.atof(line.split()[2])
                if (line.startswith('Feedback_gain')):
                    self.feedbackGain = string.atof(line.split()[1])
                if (line.startswith('Compensator_oversample')):
                    self.compOversample = string.atof(line.split()[1])
                if (line.startswith('Compensator_mode')):
                    self.compMode = line.split()[1]
                if (line.startswith('OffAxis_poly_order')):
                    self.offAxisPolyOrder = string.atoi(line.split()[1])
                if (line.startswith('Boundary_thickness')):
                    self.boundaryT = string.atoi(line.split()[2])
                if (line.startswith('Compensation_sequence')):
                    self.compSequence = np.loadtxt(os.path.join('data/algo/',line.split()[1]))
                if (line.startswith('Sumclip_sequence')):
                    self.sumclipSequence = np.loadtxt(os.path.join('data/algo/',line.split()[1]))
                if (line.startswith('Image_formation')):
                    self.imageFormation = line.split()[1]
                if (line.startswith('Minimization')):
                    self.minimization = line.split()[1]
        fid.close()
        
        if not (hasattr(self,'ZTerms')):
            self.ZTerms=np.arange(self.numTerms)+1 #starts from 1
            
        try:
            if (self.zobsR==1):
                self.zobsR=inst.obscuration
        except AttributeError:
            pass

        #if self.outerItr is large, and self.compSequence is too small, we fill in the
        #rest in self.compSequence
        #print self.compSequence.shape[0]
        if (self.compSequence.shape[0] < self.outerItr): 
            if (len(self.compSequence.shape) == 1):
                #resize compSequence to be self.outer and set all etra values to compSequence[-1]
                self.compSequence[self.compSequence.shape[0]+1:self.outerItr] = self.compSequence[-1]
            else:
                #for all dimensions resize compSequence to be self.outer and set all etra values to 1
                self.compSequence[:,self.compSequence.shape[1]+1:self.outerItr] = 1

        #if padDim==999, get the minimum padDim possible based on image size.
        try:
            if ((self.PoissonSolver == 'fft') and (self.padDim == 999)):
                self.padDim = 2**np.ceil(np.log2(inst.sensorSamples))
        except AttributeError:
            pass

        #mask scaling factor (for fast beam)
        try:
            self.maskScalingFactor = inst.focalLength/inst.marginalFL
        except AttributeError:
            self.maskScalingFactor =1

        self.caustic = 0
        self.converge = np.zeros((self.numTerms, self.outerItr + 1))
        self.debug_level=debug_level
        
    def makeMasterMask(self,I1,I2):
        self.pMask = I1.pMask*I2.pMask 
        self.cMask = I1.cMask*I2.cMask
        try:
            if (self.PoissonSolver == 'fft'):
                self.pMaskPad = padArray( self.pMask, self.padDim )
                self.cMaskPad = padArray( self.cMask, self.padDim )
        except AttributeError:
            pass

    def createSignal(self, inst,I1, I2, cliplevel):

        m1, n1 = I1.I.shape
        m2, n2 = I2.I.shape

        if( m1 != n1 ):
            raise Exception( 'EFSignal: I1 is not square' )

        if( (m1 != m2) or (n1 != n2) ):
            raise Exception( 'EFSignal: I1 and I2 are not the same size' )

        I1 = I1.I
        I2 = np.rot90(I2.I.copy(), k=2 ) #do not change m.I2 in PoissionSolver.m (

        num = I1 - I2 # -(I2-I1), the - is from S itself, see Eq.(4) of our SPIE
        den = I1 + I2

        #to apply signal_sum_clip_level
        pixelList=den*self.cMask
        pixelList[pixelList==0]=np.nan
        m1,n1= self.cMask.shape
        pixelList = np.reshape(pixelList,m1*n1)
        pixelList = pixelList[~np.isnan(pixelList)]
        low= pixelList.min()
        high= pixelList.max()
        median = (high-low)/2.+low
        den[den<median*cliplevel] = 1.5*median

        i =  den[:] == 0
        den[i] = np.inf # Forces zero in the result below.
        self.S = num / den

        c0=inst.focalLength*(inst.focalLength-inst.offset)/inst.offset
        self.S = self.S/c0

        self.S = padArray( self.S, self.padDim )*self.cMaskPad

    def getdIandI(self,I1,I2):
    
        m1, n1 = I1.I.shape
        m2, n2 = I2.I.shape

        if( m1 != n1 ):
            print( 'getdIandI: I1 is not square' )
            exit()

        if( (m1 != m2) or (n1 != n2) ):
            print( 'getdIandI: I1 and I2 are not the same size' )
            exit()

        I1 = I1.I
        I2 = np.rot90(I2.I, 2 )

        self.I = (I1+I2)/2
        self.dI = I2 - I1

    def solvePoissonEq(self,inst,I1,I2, iOutItr=0):

        if self.PoissonSolver == 'fft':
            '''Poisson Solver using an FFT
            '''
            cliplevel=self.sumclipSequence[iOutItr]#only place iOutItr is used.
            
            aperturePixelSize = (inst.apertureDiameter*inst.sensorFactor/inst.sensorSamples)
            # print "FFT"
            #    kk = np.arange(-0.5,0.5-1/self.padDim,1/self.padDim) / aperturePixelSize
            v, u = np.mgrid[-0.5/aperturePixelSize:(0.5)/aperturePixelSize:1/self.padDim/aperturePixelSize,
                            -0.5/aperturePixelSize:(0.5)/aperturePixelSize:1/self.padDim/aperturePixelSize]
    
            u2v2 = -4*(np.pi**2)*(u*u + v*v)

            # Set origin to Inf and 0 to result in 0 at origin after filtering
            ctrIdx = np.floor(self.padDim/2)
            u2v2[ctrIdx, ctrIdx] = np.inf

            self.createSignal(inst, I1,I2, cliplevel)
    
            #find the indices for a ring of pixels just ouside and just inside the
            #aperture for use in setting dWdn = 0

            struct = ndimage.generate_binary_structure(2, 1)
            struct = ndimage.iterate_structure(struct, self.boundaryT)
            #print struct
            ApringOut = np.logical_xor(ndimage.morphology.binary_dilation(self.pMask,structure=struct),self.pMask).astype(int)
            ApringIn = np.logical_xor(ndimage.morphology.binary_erosion(self.pMask,structure=struct),self.pMask).astype(int)
            #    print "ApringOut",ApringOut,np.nonzero(ApringOut)#out is different in is the same
            bordery, borderx = np.nonzero(ApringOut) # x= y and y=x
    
            if (self.compMode == 'zer'):
                zc = np.zeros((self.numTerms,self.innerItr))
                #        print "ZC ONE",zc.shape

            #*************************************************************************
            #initial BOX 3 - put signal in boundary (since there's no existing
            #Sestimate, S just equals self.S
            S = self.S.copy()
    
            for jj in range(int(self.innerItr)):
    
                #*************************************************************************
                #BOX 4 - forward filter: forward FFT, divide by u2v2, inverse FFT
                SFFT = np.fft.fftshift(np.fft.fft2( np.fft.fftshift(S) ))
                #print SFFT.shape, u2v2.shape
                W = np.fft.fftshift( np.fft.irfft2( np.fft.fftshift( SFFT/u2v2 ), s=S.shape) )

                #*************************************************************************
                #BOX 5 - Wavefront estimate (includes zeroing offset & masking to the aperture size)
                West = extractArray( W, inst.sensorSamples )
                #print "WEST", West.shape, W.shape
        
                offset = West[self.pMask==1].mean()
                West = West - offset
                West[self.pMask == 0] = 0

                if (self.compMode =='zer'):

                    zc[:,jj] = ZernikeMaskedFit(West, inst.xSensor, inst.ySensor, self.numTerms, self.pMask, self.zobsR )

                #*************************************************************************
                #BOX 6 - set dWestimate/dn = 0 around boundary
                WestdWdn0 = West.copy()
    
                #do a 3x3 average around each border pixel, including only those pixels inside the aperture
                for ii in range(len(borderx)):
                    reg = West[borderx[ii]-self.boundaryT:borderx[ii]+self.boundaryT+1,
                            bordery[ii]-self.boundaryT:bordery[ii]+self.boundaryT+1]
                    intersectIdx = ApringIn[borderx[ii]-self.boundaryT:borderx[ii]+self.boundaryT+1,
                                            bordery[ii]-self.boundaryT:bordery[ii]+self.boundaryT+1]
                    WestdWdn0[borderx[ii],bordery[ii]] = reg[np.nonzero(intersectIdx)].mean()
    
                #*************************************************************************
                #BOX 7 - Take Laplacian to find sensor signal estimate
    
                Wxx = np.zeros((inst.sensorSamples,inst.sensorSamples))
                Wyy = np.zeros((inst.sensorSamples,inst.sensorSamples))
                Wt = WestdWdn0.copy()

                Wxx[:,1:-1] = (Wt[:,0:-2]-2*Wt[:,1:-1]+Wt[:,2:])/aperturePixelSize**2
                Wyy[1:-1,:] = (Wt[0:-2,:]-2*Wt[1:-1,:]+Wt[2:,:])/aperturePixelSize**2
                del2W = Wxx + Wyy
                Sest = padArray(del2W, self.padDim)
    
                #*************************************************************************
                #BOX 3 - Put signal back inside boundary, leaving the rest of Sestimate
                Sest[self.pMaskPad==1] = self.S[self.pMaskPad==1]
                S = Sest


            self.West = West.copy()
            if (self.compMode == 'zer'):
                self.zc = zc

        elif self.PoissonSolver=='exp':
            self.getdIandI(I1,I2)
    
            xSensor=inst.xSensor*self.cMask
            ySensor=inst.ySensor*self.cMask
    
            F=np.zeros(self.numTerms)
            dZidx=np.zeros((self.numTerms,inst.sensorSamples,inst.sensorSamples))
            dZidy=dZidx.copy()
    
            aperturePixelSize = (inst.apertureDiameter*inst.sensorFactor/inst.sensorSamples)
            zcCol = np.zeros(self.numTerms)
            for i in range(int(self.numTerms)):
                zcCol[i]=1;
                #we integrate, instead of decompose, integration is faster. Also, decomposition is ill-defined on m.cMask. 
                #Using m.pMask, the two should give same results.
                if (self.zobsR>0):
                    F[i]=np.sum(np.sum(self.dI*ZernikeAnnularEval(zcCol,xSensor,ySensor,
                                                                self.zobsR)))*aperturePixelSize**2
                    dZidx[i,:,:] = ZernikeAnnularGrad(zcCol,xSensor,ySensor,self.zobsR,'dx')
                    dZidy[i,:,:] = ZernikeAnnularGrad(zcCol,xSensor,ySensor,self.zobsR,'dy')
                else:
                    F[i]=np.sum(np.sum(m.dI*ZernikeEval(zcCol,xSensor,ySensor)))*aperturePixelSize**2
                    dZidx[i,:,:]=ZernikeGrad(zcCol,xSensor,ySensor,'dx')
                    dZidy[i,:,:]=ZernikeGrad(zcCol,xSensor,ySensor,'dy')
                zcCol[i]=0
    
            self.Mij=np.zeros((self.numTerms,self.numTerms))
            for i in range(self.numTerms):
                for j in range(self.numTerms):
                    self.Mij[i,j]=aperturePixelSize**2/(inst.apertureDiameter/2)**2* \
                        np.sum(np.sum(self.I*(dZidx[i,:,:].squeeze()*dZidx[j,:,:].squeeze() \
                                                + dZidy[i,:,:].squeeze()*dZidy[j,:,:].squeeze() )))
    
            dz=2*inst.focalLength*(inst.focalLength-inst.offset)/inst.offset
            self.zc = np.zeros(self.numTerms)
            idx = [x-1 for x in self.ZTerms]
            zc_tmp = np.dot(np.linalg.pinv(self.Mij[:,idx][idx]),F[idx])/dz #phi in GN paper is phase, phi/(2pi)*lambda=W
            self.zc[idx]=zc_tmp
    
            if (self.zobsR>0):
                self.West=ZernikeAnnularEval(np.concatenate(([0,0,0],self.zc[3:]),axis=1),xSensor,ySensor,self.zobsR)
            else:
                self.West=ZernikeEval(np.concatenate(([0,0,0],self.zc[3:]),axis=1),xSensor,ySensor)

    def runIt(self,inst,I1,I2,model):
        #if we want to internally/artificially increase the image resolution
        try:
            if  ( self.upReso > 1):
                newSize=inst.sensorSamples*self.upReso
                I1.upResolution(self.upReso,newSize,newSize)
                I2.upResolution(self.upReso,newSize,newSize)
                inst.pixel_size=inst.pixel_size/self.upReso
                inst.sensorSamples = newSize
                I1.sizeinPix=newSize
                I2.sizeinPix=newSize
        except AttributeError:
            pass
        
        #pupil mask, computational mask, and their parameters
        I1.makeMaskList(inst)
        I2.makeMaskList(inst)
        I1.makeMask(inst, self.boundaryT, 1 )
        I2.makeMask(inst, self.boundaryT, 1 )
        self.makeMasterMask(I1,I2)
    
        #load offAxis correction coefficients
        if model=='offAxis':
            I1.getOffAxisCorr(self.offAxisPolyOrder)
            I2.getOffAxisCorr(self.offAxisPolyOrder)

        #cocenter the images
        I1.imageCoCenter(inst,self)
        I2.imageCoCenter(inst,self)

        # we want the compensator always start from I1_0 and I2_0
        I1_0 = copy.deepcopy(I1)
        I2_0 = copy.deepcopy(I2)

        if self.compMode == 'zer':
            ztot = np.zeros(self.numTerms)    
            if 'Axis' in model:        #onAxis or offAxis, remove distortion first
                I1.compensate(inst,self, ztot, 1, model)        
                I2.compensate(inst,self, ztot, 1, model)        

            I1, I2 = applyI1I2pMask(self,I1,I2)
            self.solvePoissonEq(inst,I1,I2,0)
            if self.PoissonSolver == 'fft':
                self.converge[:, 0] = ztot + self.zc[:, self.innerItr-1]
            elif self.PoissonSolver == 'exp':
                self.converge[:, 0] = ztot + self.zc

            if self.debug_level>=2:
                tmp=self.converge[3:,0]*1e9
                print('iter = 0, z4-z%d\n'%(self.numTerms))
                print(tmp.astype(int))

            #    m.West includes Zernikes presented by m.zc
            self.Wconverge=self.West;
            for j in range(int(self.outerItr)):
                if not self.caustic:            
                    if (self.PoissonSolver == 'fft'):
                        ztmp = self.zc[:, -1]
                    else:                
                        ztmp = self.zc                

                    if (self.compSequence.ndim == 1):                
                        ztmp[self.compSequence[j]:] = 0                
                    else:                
                        ztmp = ztmp * self.compSequence[:, j]
            
                    ztot = ztot + ztmp * self.feedbackGain
                    
                    I1 = copy.deepcopy(I1_0)
                    I2 = copy.deepcopy(I2_0)
  
                    I1.compensate(inst,self, ztot, 1, model)        
                    I2.compensate(inst,self, ztot, 1, model)        
                    if (I1.caustic==1 or I2.caustic==1):
                        self.caustic=1
                    I1, I2 = applyI1I2pMask(self,I1,I2)
                    self.solvePoissonEq(inst,I1,I2,j)
                    if self.PoissonSolver == 'fft':
                        self.converge[:, j+1] = ztot + self.zc[:, self.innerItr-1]
                    elif self.PoissonSolver == 'exp':
                        self.converge[:, j+1] = ztot + self.zc

                    if self.debug_level>=2:
                        tmp=self.converge[3:,j+1]*1e9
                        print('iter = %d, z4-z%d\n'%(j+1,self.numTerms))
                        print(tmp.astype(int))
                        
                    #m.West is the estimated wavefront from the last run of Poisson
                    #solver. ztot is what had be compensated before that run.
                    #m.West includes two parts: latest m.zc, and m.Wres
                    #m.Wres is the residual wavefront on top of m.converge(:,end),
                    #m.Wres is only available for the iterative FFT algorithm.
                    if (self.zobsR==0):
                        self.Wconverge=ZernikeEval(np.concatenate(([0,0,0],ztot[3:]),axis=1),\
                                            inst.xSensor,inst.ySensor)+self.West
                    else:
                        self.Wconverge=ZernikeAnnularEval(np.concatenate(([0,0,0],ztot[3:]),axis=1),\
                                                   inst.xSensor,inst.ySensor,self.zobsR)+self.West;
                else:            # once we run into caustic, stop here, results may be
                        # close to real aberration. Continuation may lead to disatrous results
                    self.converge[:, j+1] = self.converge[:, j]

        elif (self.compMode == 'opd'):
            wtot = np.zeros(inst.sensorSamples, inst.sensorSamples)    
        
            self.caustic = 0    
            if 'Axis' in model:        #onAxis or offAxis, remove distortion first
                I1.compensate(inst,self, wtot, 1, model)        
                I2.compensate(inst,self, wtot, 1, model)        
            I1, I2 = applyI1I2pMask(self,I1,I2)
            self.solvePoissonEq(inst,I1,I2,0)
            Wconverge = wtot + self.West    
            self.converge[:, 0] = ZernikeMaskedFit(Wconverge, inst.xSensor, inst.ySensor,
                                                   self.numTerms, self.pMask, self.zobsR)    

            for j in range(int(self.outerItr)):
                if not self.caustic: 
                    wtmp = self.West            
                    wtot = wtot + wtmp * self.feedbackGain            

                    I1 = copy.deepcopy(I1_0)
                    I2 = copy.deepcopy(I2_0)
                    I1.compensate(inst,self, wtot, 1, model)        
                    I2.compensate(inst,self, wtot, 1, model)        
                    if (I1.caustic==1 or I2.caustic==1):
                        self.caustic=1
                    I1, I2 = applyI1I2pMask(self,I1,I2)
                    self.solvePoissonEq(inst,I1,I2,j)

                    Wconverge = wtot + self.West            
                    self.converge[:, j] = ZernikeMaskedFit(Wconverge, inst.xSensor, inst.ySensor,
                                                           self.numTerms,self.pMask, self.zobsR)            
                else:            # once we run into caustic, stop here, results may be
                        # close to real aberration. Continuation may lead to disatrous results
                    self.converge[:, j+1] = self.converge[:, j]

    def outZer4Up(self,filename,unit):
        z=getZer4Up(self,unit)
        if (filename==''):
            f = sys.stdout
        else:
            f = open(filename, 'w')

        for i in range(4,self.numTerms+1):
            f.write('%d\t %8.1f\n'%(i,z[i-4]))
        if not (filename==''):
            f.close()
            
    def showSignal(self):
        try:
            plt.imshow(self.S,origin='lower')
            plt.colorbar()
            plt.title('Wavefront signal')
            plt.show()
        except AttributeError:
            print('Wavefront signal is not available')
            if self.PoissonSolver=='exp':
                print('because you are using -a exp')
            else:
                print('because you have not calculated it.')
                
def getZer4Up(algo,unit):
    if unit=='m':
        return algo.converge[3:,-1]
    elif unit=='nm':
        return algo.converge[3:,-1]*1e9
    elif unit == 'um':
        return algo.converge[3:,-1]*1e6
    
def applyI1I2pMask(algo,I1,I2):
    if (I1.fldx != I2.fldx or I1.fldy != I2.fldy):        
        I1.I = I1.I * algo.pMask            
        I2.I = I2.I * np.rot90(algo.pMask, 2)
        I1.I = I1.I / np.sum(I1.I)            
        I2.I = I2.I / np.sum(I2.I)        #no need vignetting correction, this is after masking already
    return I1, I2

