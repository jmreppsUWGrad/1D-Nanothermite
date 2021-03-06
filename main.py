# -*- coding: utf-8 -*-
"""
######################################################
#             1D Heat Conduction Solver              #
#              Created by J. Mark Epps               #
#          Part of Masters Thesis at UW 2018-2020    #
######################################################

This file contains the main executable script for solving 1D conduction:
    -Uses FileClasses.py to read and write input files to get settings for solver
    and geometry
    -Creates a domain class from GeomClasses.py
    -Creates solver class from SolverClasses.py with reference to domain class
    -Can be called from command line with: 
        python main.py [Input file name+extension] [Output directory relative to current directory]
    -Calculates the time taken to run solver
    -Changes boundary conditions based on ignition criteria
    -Saves temperature data (.npy) at intervals defined in input file
    -Saves x grid array (.npy) to output directory

Features:
    -Ignition condition met, will change north BC to that of right BC
    -Saves temperature and reaction data (.npy) depending on input file 
    settings

"""

##########################################################################
# ----------------------------------Libraries and classes
##########################################################################
import numpy as np
import string as st
#from datetime import datetime
import os
import sys
import time
import copy
from mpi4py import MPI

import GeomClasses as Geom
import SolverClasses as Solvers
import FileClasses
import mpi_routines

##########################################################################
# -------------------------------------Beginning
##########################################################################

comm = MPI.COMM_WORLD
rank = comm.Get_rank()
size = comm.Get_size()

# Print intro on process 0 only
if rank==0:
    print('######################################################')
    print('#             1D Heat Conduction Solver              #')
    print('#              Created by J. Mark Epps               #')
    print('#          Part of Masters Thesis at UW 2018-2020    #')
    print('######################################################\n')
    
    # Start timer
    time_begin=time.time()

# Get arguments to script execution
settings={}
BCs={}
Sources={}
Species={}
inputargs=sys.argv
if len(inputargs)>2:
    input_file=inputargs[1]
    settings['Output_directory']=inputargs[2]
else:
    if rank==0:
        print 'Usage is: python main.py [Input file] [Output directory]\n'
        print 'where\n'
        print '[Input file] is the name of the input file with extension; must be in current directory'
        print '[Output directory] is the directory to output the data; will create relative to current directory if it does not exist'
        print '***********************************'
    sys.exit('Solver shut down on %i'%(rank))
##########################################################################
# -------------------------------------Read input file
##########################################################################
if rank==0:
    print 'Reading input file...'
fin=FileClasses.FileIn(input_file, 0)
fin.Read_Input(settings, Sources, Species, BCs)
settings['MPI_Processes']=size
try:
    os.chdir(settings['Output_directory'])
except:
    if rank==0:
        os.makedirs(settings['Output_directory'])
        err=0
    else:
        err=1
    err=comm.bcast(err, root=0) # Way of syncing processes
    os.chdir(settings['Output_directory'])
#print '****Rank: %i has read input file'%(rank)
##########################################################################
# -------------------------------------Initialize solver and domain
##########################################################################
if rank==0:
    print '################################'
    print 'Initializing geometry package...'
domain=Geom.OneDimLine(settings, Species, 'Solid', rank)
domain.mesh()
hx=domain.CV_dim()
if rank==0:
    print '################################'
    print 'Initializing MPI and solvers...'
    np.save('X', domain.X, False)
mpi=mpi_routines.MPI_comms(comm, rank, size, Sources, Species)
err=mpi.MPI_discretize(domain)
if err>0:
    sys.exit('Problem discretizing domain into processes')
#print '****Rank: %i, x array : '%(rank)+str(domain.X)
hx=mpi.split_var(hx, domain)

domain.create_var(Species)
solver=Solvers.OneDimLineSolve(domain, settings, Sources, copy.deepcopy(BCs), 'Solid', size, comm)
if rank==0:
    print '################################'
    print 'Initializing domain...'

time_max='0.000000'
T=300*np.ones_like(domain.E)
#T=np.linspace(300, 600, len(domain.E))
# Restart from previous data
if st.find(settings['Restart'], 'None')<0:
    times=os.listdir('.')
    i=len(times)
    if i<2:
        sys.exit('Cannot find a file to restart a simulation with')
    j=0
    while i>j:
        if st.find(times[j],'T')==0 and st.find(times[j],'.npy')>0 \
            and st.find(times[j],str(settings['Restart']))>=0:
            times[j]=st.split(st.split(times[j],'_')[1],'.npy')[0]
#            if st.find(times[j],str(settings['Restart']))>=0:
            time_max=times[j]
            j+=1
            break
        else:
            del times[j]
            i-=1
    
    T=np.load('T_'+time_max+'.npy')
    T=mpi.split_var(T, domain)
    if st.find(Sources['Source_Kim'],'True')>=0:
        eta=np.load('eta_'+time_max+'.npy')
        domain.eta=mpi.split_var(eta, domain)
        del eta
    if domain.model=='Species':
        P=np.load('P_'+time_max+'.npy')
        domain.P=mpi.split_var(P, domain)
        species=['g','s']
        for i in range(len(species)):
            rho_species=np.load('rho_'+species[i]+'_'+time_max+'.npy')
            domain.rho_species[species[i]]=mpi.split_var(rho_species, domain)
        del rho_species, P
    
rhoC=domain.calcProp(T_guess=T, init=True)

domain.E=rhoC*T
del rhoC,T
#print 'Rank %i has initialized'%(rank)
###########################################################################
## ------------------------Write Input File settings to output directory (only process 0)
###########################################################################
if rank==0:
    print '################################'
    print 'Saving input file to output directory...'
    #datTime=str(datetime.date(datetime.now()))+'_'+'{:%H%M}'.format(datetime.time(datetime.now()))
    isBinFile=False
    
    input_file=FileClasses.FileOut('Input_file', isBinFile)
    
    # Write header to file
    input_file.header_cond('INPUT')
    
    # Write input file with settings
    input_file.input_writer_cond(settings, Sources, Species, BCs)
    print '################################\n'

    print 'Saving data to numpy array files...'
mpi.save_data(domain, time_max)

###########################################################################
## -------------------------------------Solve
###########################################################################
t,nt,tign=float(time_max)/1000,0,0 # time, number steps and ignition time initializations
v_0,v_1,v,N=0,0,0,0 # combustion wave speed variables initialization
dx=mpi.compile_var(domain.dx, domain)

# Setup intervals to save data
output_data_t,output_data_nt=0,0
if settings['total_time_steps']=='None':
    output_data_t=settings['total_time']/settings['Number_Data_Output']
    settings['total_time_steps']=settings['total_time']*10**12
    t_inc=int(t/output_data_t)+1
elif settings['total_time']=='None':
    output_data_nt=int(settings['total_time_steps']/settings['Number_Data_Output'])
    settings['total_time']=settings['total_time_steps']*10**12
    t_inc=0

# Ignition conditions
ign,ign_0=0,0

if rank==0:
    print 'Solving:'
while nt<settings['total_time_steps'] and t<settings['total_time']:
    # First point in calculating combustion propagation speed
#    T_0=domain.calcProp()[0]
#    print 'Rank %i has reached while loop'%(rank)
    if st.find(Sources['Source_Kim'],'True')>=0 and ign==1:
        eta=mpi.compile_var(domain.eta, domain)
        if rank==0:
            v_0=np.sum(eta*dx)
       
    # Update ghost nodes
    mpi.update_ghosts(domain)
    # Actual solve
    err,dt,ign=solver.Advance_Soln_Cond(nt, t, hx,ign)
    t+=dt
    nt+=1
    # Check all error codes and send the maximum code to all processes
    err=comm.reduce(err, op=MPI.MAX, root=0)
    err=comm.bcast(err, root=0)
    # Check all ignition codes and send maximum
    ign_0=ign
    ign_0=comm.reduce(ign_0, op=MPI.MIN, root=0)
    ign_0=comm.bcast(ign_0, root=0)
    ign=comm.reduce(ign, op=MPI.MAX, root=0)
    ign=comm.bcast(ign, root=0)
    
    if err>0:
        if rank==0:
            print '#################### Solver aborted #######################'
            print '#################### Error: %i'%(err)
            print 'Error codes: 1-time step, 2-Energy, 3-reaction progress, 4-Species balance'
            print 'Saving data to numpy array files...'
            input_file.Write_single_line('#################### Solver aborted #######################')
            input_file.Write_single_line('Time step %i, Time elapsed=%f, error code=%i;'%(nt,t,err))
            input_file.Write_single_line('Error codes: 1-time step, 2-Energy, 3-reaction progress, 4-Species balance')
        mpi.save_data(domain, '{:f}'.format(t*1000))
        break
    
    # Output data to numpy files
    if (output_data_nt!=0 and nt%output_data_nt==0) or \
        (output_data_t!=0 and (t>=output_data_t*t_inc and t-dt<output_data_t*t_inc)):
        if rank==0:
            print 'Saving data to numpy array files...'
        mpi.save_data(domain, '{:f}'.format(t*1000))
        t_inc+=1
        
    # Change boundary conditions and calculate wave speed
    if ign==1 and ign_0==0:
        if domain.proc_left<0:
            solver.BCs.BCs['bc_left_E']=BCs['bc_right_E']#['C', (30, 300), (0,-1)]
        if rank==0:
            input_file.fout.write('##bc_left_E_new:')
            input_file.Write_single_line(str(solver.BCs.BCs['bc_left_E']))
            input_file.fout.write('\n')
            tign=t
        mpi.save_data(domain, '{:f}'.format(t*1000))
        
    # Second point in calculating combustion propagation speed
    if st.find(Sources['Source_Kim'],'True')>=0 and ign==1:
        eta=mpi.compile_var(domain.eta, domain)
        if rank==0:
            v_1=np.sum(eta*dx)
            if (v_1-v_0)/dt>0.001:
                v+=(v_1-v_0)/dt
                N+=1
        
if rank==0:        
    time_end=time.time()
    input_file.Write_single_line('Final time step size: %f ms'%(dt*1000))
    print 'Ignition time: %f ms'%(tign*1000)
    input_file.Write_single_line('Ignition time: %f ms'%(tign*1000))
    print 'Solver time per 1000 time steps: %f min'%((time_end-time_begin)/60.0*1000/nt)
    input_file.Write_single_line('Solver time per 1000 time steps: %f min'%((time_end-time_begin)/60.0*1000/nt))
    input_file.Write_single_line('Total time steps: %i'%(nt))
    try:
        print 'Average wave speed: %f m/s'%(v/N)
        input_file.Write_single_line('Average wave speed: %f m/s'%(v/N))
        input_file.close()
    except:
        print 'Average wave speed: 0 m/s'
        input_file.Write_single_line('Average wave speed: 0 m/s')
        input_file.close()
    print('Solver has finished its run')