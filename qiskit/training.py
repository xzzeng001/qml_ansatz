# for parallel
import multiprocessing as mp
num_proc=mp.cpu_count()
import os
os.environ["OMP_NUM_THREADS"] = str(num_proc+1)

from qiskit import Aer
from qiskit.utils import QuantumInstance
from qiskit.opflow.converters import CircuitSampler

import save_data as sd
from ansatz_classes import Ansatz_Pool,generate_network_parameters
import ansatz_classes as ac
import sys
from pyscf_func import chop_to_real
import os
from qiskit.algorithms.optimizers import ADAM,COBYLA,P_BFGS,NFT,QNSPSA,NELDER_MEAD
import ray
#import functools

# User configuration
from user_config import provider_info

# General imports
from copy import deepcopy
from time import time

# Avoid thousands of INFO logging lines
import logging
import qiskit # type: ignore
qiskit.transpiler.passes.basis.basis_translator.logger.setLevel(logging.ERROR)
qiskit.transpiler.runningpassmanager.logger.setLevel(logging.ERROR)

# ---- QISKIT ----
from qiskit import QuantumCircuit, QuantumRegister, ClassicalRegister, IBMQ,assemble
from qiskit.quantum_info.states.utils import partial_trace # type: ignore
from qiskit.quantum_info.random import random_unitary, random_statevector # type: ignore
from qiskit.providers.ibmq import least_busy # type: ignore
from qiskit.circuit.library.standard_gates import U3Gate, RXGate, RYGate, XGate # type: ignore
from qiskit.providers.aer import QasmSimulator # type: ignore

# additional math libs
import numpy as np # type: ignore
import scipy
from scipy.constants import pi # type: ignore
from scipy.linalg import expm # type: ignore

# Typing
from typing import Union, Optional, List, Tuple, Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# global device variable for simulator=True
DEVICE = None

def generate_circuits(ansatz: Union[Ansatz_Pool],
                      all_params: Any = [],
                      circuits_type: str = "ep_circuits",
                      shots: int = 2**13,
                      idx_circuits: int=0) -> dict:
    """
    Generates a dictionary including executable circuits given a ansatz type,
    the parameters, and some other optionable properties.

    Args:
        ansatz (Union[Ansatz_Pool]): ansatz class object.
        all_params (Any, optional): 2-dimensional with 1-d lists of ansatz parameters. Defaults to [].
        circuits_type (str, optional): Type of circuit, depending on the circuits' use case. Defaults to "ep_circuits".
        shots (int, optional): Number of shots. Automatically multiplies circuits if (shots > 2^13). Defaults to 2^13.
        draw_circ (bool, optional): Set if the transpiled circuit should be saved as a png file. Defaults to False.

    Returns:
        dict: Including the properties circuits_type, ap_circuits (with assigned parameters),
              num_states (e.g. 4 training pairs), and loops (e.g. 2 for 2^13 shots)
              This dictionary can be fed into execute_circuits()
    """    
    
    # Check and if necessary correct shape of parameter list
    if len(np.shape(all_params)) != 2:
        all_params = [all_params]
    
    # Helper evariable1s that enables to execute circuits with shots > 2^14
    loop, shots = number_of_loops(shots)
    
    # Get parametrized circuit (parameters not assigned yet)
    circuits = ansatz.ep_circuits[idx_circuits]

    # Generate circuits with assigned parameters
    ap_circuits: List[QuantumCircuit] = []
    for ansatz_params in all_params:
        for circuit in circuits: # loop over training/validation pairs
            for _ in range(loop):
                ap_circuits.append(circuit.assign_parameters({ansatz.param_vector: ansatz_params}))

    return {'circuits_type': circuits_type+str(idx_circuits), 'circuits': ap_circuits, 'num_states': len(circuits), 'loops': loop}

def execute_circuits(ansatz: Union[Ansatz_Pool],
                     circuits: List[Any],
                     simulator: bool,
                     device_name: str,
                     idx_circuit: int = 0,
                     epoch: int = 0,
                     shots: int = 2**13) -> float:
    """
    Executes a single or a list of circuits on either the
    simulator or a real device with the given number of shots.

    Args:
        ansatz (Union[Ansatz_Pool, Network_QAOA]): Network class object.
        circuits (List[Any]): List of dictionaries including executable circuits. Generated by generate_circuits().
        simulator (bool): Tells the function if a simulator is used or a real device
        device_name (str): Choose an execution device by name.
        epoch (int, optional): Epoch of learning, used to identify calibrations. Defaults to 0.
        shots (int, optional): Number of times a single circuit is run. Defaults to 2^13.

    Returns:
        List[List[float]]: Returns the averaged cost for every different parameter set in the following form [[training_cost_old, paramset_1_training, paramset_2_training,...], [validation_cost], [identity_cost], [fidelity_cost]]
    """
        
    # Check and if necessary correct shape of circuit dict list
    if not isinstance(circuits[0], dict):
        circuits = [circuits]
    
    # Define device
    if simulator:
        # Fix device to first calibration
        global DEVICE
        device = DEVICE or get_device(ansatz, simulator, epoch, device_name=device_name)
        DEVICE = device
    else:
        # Get device and download calibration data for each epoch
        device = get_device(ansatz, simulator, epoch, device_name=device_name)
   
    # Helper variable that enables to execute circuits with shots > 2^14
    shots = number_of_loops(shots)[1]
    
    # Flatten the circuits but remember the shape -> Execute different circuit types within one request
    shape: List[int] = []
    flat_circuits: List[QuantumCircuit] = []
    for k, circ in enumerate(circuits):
        shape.append(shape[k-1]+len(circ.get('circuits')) if k > 0 else len(circ.get('circuits')))
        flat_circuits.extend(circ.get('circuits'))
    shape = shape[:-1] # Remove last index for better use with np.split()

    ii=0
    for circ in flat_circuits:
        file_='circuit_'+str(ii)+'.txt'
        circ.qasm(filename=file_)
        ii+=1
    sys.exit(0)

    # Execute all circuits on device
    counts = ansatz.execute_circuits(flat_circuits, device, shots)

#    print('counts',counts)
   
    # Post-processing of the measurement results
    if not isinstance(counts, list): counts = [counts]

    c0 = np.asarray([count.get('0', 0) for count in counts])
    c1 = np.asarray([count.get('1', 0) for count in counts])
    flat_costs = (c0-c1)/shots 

    # Sort costs into list for different circuit types
    avg_costs = [[np.average(c) for c in np.split(costs,len(costs)//(circuits[i].get('loops') * circuits[i].get("num_states")))] for i, costs in enumerate(np.split(flat_costs, shape))]

    return float(ansatz.ep_cont[idx_circuit+1])*float(avg_costs[0][0])

def get_cost_from_counts(ansatz: Union[Ansatz_Pool],
                         counts: List[dict],
                         shots: int) -> List[float]:
    """
    Classical post-processing to evaluate fidelity from the destructive swap's measurement.

    Args:
        ansatz (Union[Ansatz_Pool, Network_QAOA]): Network class object.
        counts (List[dict]): Counts as returned from ansatz.execute_circuits().
        shots (int): Actually used Number of shots.

    Returns:
        List[float]: Corresponding costs to counts.
    """    
    c01 = np.zeros((2,len(counts)), dtype=int)
    
    # Calulate fidelity
    # counts looks like [{"01": 1024, "11": 1023, "00": 1}, ...] with one dict for every circuit
    for j, count in enumerate(counts):
        for state, c in count.items():
            input_state_vec = np.asarray(list(map(int, state[:len(state)//2])))
            output_state_vec = np.asarray(list(map(int, state[len(state)//2:])))
            res = np.dot(input_state_vec, output_state_vec) % 2
            c01[res][j] += c
    return (c01[0]-c01[1])/shots

def get_provider() -> Any:
    """
    Get IBMQ provider as defined in user_config. Fallback if user has no access.

    Returns:
        Any: IBMQ provider
    """
    try:
        # Get provider as defined in user_config
        return IBMQ.get_provider(hub=provider_info['hub'], group=provider_info['group'], project=provider_info['project'])
    except:
        # Fallback if user has no access to the upper provider
        return IBMQ.get_provider(hub='ibm-q')

def get_device(ansatz: Union[Ansatz_Pool], 
               simulator: bool = True,
               epoch: int = 0,
               do_calibration: bool = True,
               device_name: Optional[str] = None) -> Any:
    """
    If simulator == False:
        if device_name given:
            returns requested device
        else:
            returns the backend of the least busy functional device
    else:
        if device_name given:
            returns a noisy simulator corresponding to the given device_name
        else:
            returns the backend "qasm_simulator" without noise

    Args:
        ansatz (Union[Ansatz_Pool, Network_QAOA]): Network object.
        simulator (bool, optional): If it should run on a simulator. Defaults to True.
        epoch (int, optional): Epoch of learning, used to identify calibrations. Defaults to 0.
        device_name (str, optional): Choose an execution device by name. Defaults to None.

    Returns:
        Any: Backend of the least busy device or simulator.
    """ 
    # Simulated device or raw simulator   
    if simulator:
        # device_name is "qasm_simulator(ibmq_athens)" after calling simulator=True and device_name="ibmq_athens"
        # -> check if "qasm_simulator(" is in the name to distinguish simulated real device from simulator
        if device_name and (not 'qasm_simulator' in device_name or "qasm_simulator(" in device_name) and (not "aer_simulator" in device_name):
            provider = get_provider()
            # if there is a bracket in the name, take the device name inside of it
            if "(" in device_name:
                device_name = device_name[15:-1]
            backend = provider.get_backend(device_name)
            simulated_backend = QasmSimulator.from_backend(backend)
            sd.save_calibration_info_from_backend(backend, epoch=epoch)
            return simulated_backend
        backend = Aer.get_backend(device_name)
        return backend
    
    # real device
    provider = get_provider()
    if (device_name):
        backend = provider.get_backend(device_name)
    else :
        min_qubits = ansatz.required_qubits
        least_busy_device = least_busy(provider.backends(filters=lambda x: x.configuration().n_qubits >= min_qubits and 
                                not x.configuration().simulator and x.status().operational==True))
        backend = provider.get_backend(least_busy_device.name())
    sd.save_calibration_info_from_backend(backend, epoch=epoch)
    return backend
        
def number_of_loops(shots: int, shots_per_job: int = 2**13) -> Tuple[int, int]:
    """
    Helper function that enables to execute circuits with shots > 2^14.
    Simply calculates the loops necessary to reach shots with maximal number of shots_per_job.
    If shots <= 2^13 it simply returns loops=1 and shots as given

    Args:
        shots (int): Original and desired number of executed shots
        shots_per_job (int, optional): Maximally possible number of shots per job. Defaults to 2**13.

    Returns:
        Tuple[int, int]: Loops and number of shots per job
    """    
    loops = 1
    if (shots > shots_per_job):
        loops = shots//shots_per_job # makes int
        shots = shots_per_job
    return loops, shots

def make_diff_params(all_params: List[List[float]],
                     epsilon: float,
                     order_of_derivative: int = 2) -> List[List[float]]:
    """
    Takes the list all_params that should look like [[param1, param2, ...]] and appends
    lists of all parameters with one parameter changed by
        + epsilon at a time and
        - epsilon at a time (if order_in_epsilon == 2)

    Args:
        all_params (List[List[float]]): Basis list of all original parameters that gets expanded.
        epsilon (float): Value by which parameter is changed by.
        order_in_epsilon (int, optional): Order of the derivative of the cost function. Defaults to 1.

    Returns:
        List[List[float]]: List of all parameter sets, inluding the original and the changed (one parameter at a time).
                            Its shape is (len(all_params)*order_in_epsilon + 1,len(all_params[0])).
    """
    assert order_of_derivative in [1,2], "order_of_derivative should be eiter 1 or 2."
    for sign in [+1, -1][:order_of_derivative]:
        for i in range(len(all_params[0])):
            new_params = deepcopy(all_params[0])
            new_params[i] += sign * epsilon
            all_params.append(new_params)
    return all_params

def normal_vqe(ansatz: Union[Ansatz_Pool],
                  device_name: str,
                  epochs: int=1000,
                  simulator: bool=True,
                  shots: int=2**13,
                  optimize_method: str="COBYLA",
                  analy_grad: bool=True,
                  simulation_method: str="matrix_product_state",
                  order_of_derivative: int=2,
                  epsilon: float=0.25,
                  expectation: Optional[Any]=None,
                  learning_rate: Optional[float]=1e-3) -> float:
    """
    Trains the given ansatz for the given amount of epochs.
    Simultaneously calculates an identity cost for comparison and optionally a validation cost.
    Plots the parameters and cost for each epoch.

    Args:
        ansatz (Union[Ansatz_Pool, Network_QAOA]): Network object.
        device_name (str): Choose a specific IBMQ device by name. All epochs will be executed by the given device.
        epochs (int, optional): Number of learning epochs. Defaults to 10.
        simulator (bool, optional): Whether the simulator should be used. Defaults to True.
        shots (int, optional): How many times the device should repeat its measurement. Defaults to 2^13.
        optimize_method (str, optional): Used gradient method. Defaults to 'gradient_descent'.
    Returns:
        float: The final optimize result.
    """
#    start_time = time()

    # Define BOOKKEEPING lists
    plot_list_cost: List[List[Union[float]]] = []

    all_params = [ansatz.params]

    # Define device
    if simulator:
        backend = Aer.get_backend(device_name)
        backend.set_option("method",simulation_method)
        backend.set_option("max_parallel_experiments",num_proc)
    else:
        # Get device and download calibration data for each epoch
        backend = get_device(ansatz, simulator, epochs, device_name=device_name)
   
    if ansatz.gate_error_probabilities:
        q_instance = QuantumInstance(backend, shots=shots,coupling_map=ansatz.coupling_map, noise_model=ansatz.noise_model)
    else: 
        q_instance = QuantumInstance(backend, shots=shots)
 
    def calculate_energy(ansatz_params):
        ep_final=float(ansatz.ep_cont[0])
        # get training cost circuits
#        start_time = time()
        for ii in range(ansatz.ep_ncircuits):
            circuits=[]
            circuits.append(generate_circuits(ansatz, ansatz_params, "ep_circuits", shots, ii))
            ep_final += execute_circuits(ansatz, circuits, simulator, device_name, ii, shots)

#        end_time = time()
#        print('total_time:',end_time-start_time)
        plot_list_cost.append([ep_final,ansatz.fci_e,ep_final-ansatz.fci_e])
        if abs(ep_final-ansatz.fci_e) < abs(ansatz.min_e-ansatz.fci_e):
            ansatz.min_e=ep_final
#            all_params_epochs.append([ansatz_params])
            sd.save(ansatz=ansatz, all_params_epochs=[ansatz_params], plot_list_cost=plot_list_cost)
        else:
            sd.save(plot_list_cost=plot_list_cost)

        del circuits

    def calculate_expectation(ansatz_params):
        # assign the parameters
#        start_time = time()

        params={ansatz.param_vector[i]: ansatz_params[i] for i in range(len(ansatz_params))}
        sampler = CircuitSampler(q_instance).convert(expectation,params)
        ep_final = sampler.eval().real

#        end_time = time()
#        print('total_time:',end_time-start_time)
#        sys.exit(0)
        plot_list_cost.append([ep_final,ansatz.fci_e,ep_final-ansatz.fci_e])

        if abs(ep_final-ansatz.fci_e) < abs(ansatz.min_e-ansatz.fci_e):
            ansatz.min_e=ep_final            
#            all_params_epochs.append([ansatz_params])
            sd.save(ansatz=ansatz, all_params_epochs=[ansatz_params], plot_list_cost=plot_list_cost)
        else:
            sd.save(plot_list_cost=plot_list_cost)
            
        return ep_final

    def calculate_derivative(ansatz_params):
        # generate the desied parameters
        new_params=[ansatz_params]
#        start_time = time()

        val_list=[]
        # assign the parameters and collect the results
        params={ansatz.param_vector[i]: ansatz_params[i] for i in range(len(ansatz_params))}
        sampler = CircuitSampler(q_instance).convert(expectation,params)
        val_old=sampler.eval().real

        # define the sampling function   
        @ray.remote
        def sample_energy(new_params,epsilon, param_vector,CircuitSampler,q_instance,expectation,ii):
            if ii < len(new_params[0]):
                sign = 1
                jj = ii
            else:
                sign = -1
                jj = ii-len(new_params[0])

            circuit_params = deepcopy(new_params[0])
            circuit_params[jj] += sign * epsilon
            params={param_vector[i]: circuit_params[i] for i in range(len(new_params[0]))}
            sampler = CircuitSampler(q_instance).convert(expectation,params)

            return sampler.eval().real
 
        futures = [sample_energy.remote(new_params,epsilon,ansatz.param_vector,CircuitSampler,
                    q_instance,expectation, ii) for ii in range(2*len(new_params[0]))]
        val_list=ray.get(futures)

#        print('how many processors in used:',mp.cpu_count()) 
#        num_proc=mp.cpu_count()
#        end_time = time()
#        print('total_time:',end_time-start_time)
#        sys.exit(0)
        
        # calculate the derivatives
#        print('values:',val_old)
        if (order_of_derivative == 1):
            val_diffs = (val_list[0:len(val_list)] - np.full_like(val_list[0:len(val_list)],1)*val_old)/epsilon # only + epsilon
        elif (order_of_derivative == 2):
            # (C(x+epsilon) - C(x-epsilon)) / 2epsilon
            val_diffs = (np.subtract(val_list[0:(len(val_list))//2], val_list[(len(val_list))//2:]))/(2*epsilon)
        val_diffs = np.array(val_diffs)

        plot_list_cost.append([val_old,ansatz.fci_e,val_old-ansatz.fci_e])

        if abs(val_old-ansatz.fci_e) < abs(ansatz.min_e-ansatz.fci_e):
            ansatz.min_e=val_old
            sd.save(ansatz=ansatz, all_params_epochs=[ansatz_params], plot_list_cost=plot_list_cost)
        else:
            sd.save(plot_list_cost=plot_list_cost)
     
        return val_diffs

    # start the ray 
    if analy_grad:
        ray.init()

    ## for the main training process
    if expectation == None:
        result = scipy.optimize.minimize(
             calculate_energy, all_params[0],method=optimize_method, tol=1e-6, options={"disp": True,"maxiter": epochs})
    else:
        xx=all_params[0]
        if optimize_method == "COBYLA":
            rtmp=COBYLA(maxiter=epochs).optimize(num_vars=len(xx),objective_function=calculate_expectation,initial_point=xx,
                       gradient_function=calculate_derivative if analy_grad else None)
        elif optimize_method == "Nelder-Mead":
            rtmp=NELDER_MEAD(maxiter=epochs,adaptive=True).optimize(num_vars=len(xx),objective_function=calculate_expectation,
                       initial_point=xx,gradient_function=calculate_derivative if analy_grad else None)
        elif optimize_method == "Adam":
            rtmp=ADAM(maxiter=epochs,lr=learning_rate).optimize(num_vars=len(xx),objective_function=calculate_expectation,initial_point=xx,gradient_function=calculate_derivative if analy_grad else None)
        elif optimize_method == "QNSPSA":
            fidelity = QNSPSA.get_fidelity(ansatz.psi)
            qnspsa = QNSPSA(fidelity, maxiter=epochs)
            result = qnspsa.optimize(num_vars=len(xx),objective_function=calculate_expectation,initial_point=xx,
                              gradient_function=calculate_derivative if analy_grad else None)
 
    if analy_grad:
        ray.shutdown()

    return sd.find_minimum()

def adapt_vqe(ansatz: Union[Ansatz_Pool],
                  device_name: str,
                  epochs: int=1000,
                  simulator: bool=True,
                  shots: int=2**13,
                  optimize_method: str="COBYLA",
                  analy_grad: bool=True,
                  simulation_method: str="matrix_product_state",
##                  expectation: Optional[Any]=None,
                  learning_rate: Optional[float]=1e-3,
                  ucc_operator_pool_qubitOp:Optional[Any]=None,
                  adapt_tol: float=1e-1,
                  adapt_max_iter: int=20) -> float:
    """
    Trains the given ansatz for the given amount of epochs.
    Simultaneously calculates an identity cost for comparison and optionally a validation cost.
    Plots the parameters and cost for each epoch.

    Args:
        ansatz (Union[Ansatz_Pool, Network_QAOA]): Network object.
        device_name (str): Choose a specific IBMQ device by name. All epochs will be executed by the given device.
        epochs (int, optional): Number of learning epochs. Defaults to 10.
        simulator (bool, optional): Whether the simulator should be used. Defaults to True.
        shots (int, optional): How many times the device should repeat its measurement. Defaults to 2^13.
        optimize_method (str, optional): Used gradient method. Defaults to 'gradient_descent'.
    Returns:
        float: The final optimize result.
    """

    # Define BOOKKEEPING lists
#    all_params_epochs = []
    plot_list_cost: List[List[Union[float]]] = []

    backend = Aer.get_backend(device_name)
    backend.set_option("method",simulation_method)
    backend.set_option("max_parallel_experiments",num_proc)
    q_instance = QuantumInstance(backend, shots=shots)

    def calculate_expectation(ansatz_params,expectation):
        # assign the parameters
#        start_time = time()

        params={ansatz.param_vector[i]: ansatz_params[i] for i in range(len(ansatz_params))}
        sampler = CircuitSampler(q_instance).convert(expectation,params)
        ep_final = sampler.eval().real

#        end_time = time()
#        print('total_time:',end_time-start_time)
#        sys.exit(0)

#        print('params:',params)        
#        print('ep_final:',ep_final)   
        return ep_final

    n_operators = len(ucc_operator_pool_qubitOp)
    hamiltonian_qubitOp=ansatz.hamiltonian

    def commutator(op0, op1):
        return op0 * op1 - op1 * op0

    commutator_list = [
        commutator(ucc_operator_pool_qubitOp[i], hamiltonian_qubitOp)
        for i in range(n_operators)]

    new_operator_pool = []
    new_amplitudes = []
    new_energy = 0.
    for ii in range(adapt_max_iter):
        print("ADAPT iteration %d:" % (ii + 1))

#        print('new_operator_pool:',new_operator_pool)
        ansatz.num_params =len(new_operator_pool)
        # Calculate residual gradient
        residual_gradients_i = np.zeros(n_operators)
        for op_idx in range(n_operators):
            # Let the Hamiltonian be commutator_list[op_idx]
            ansatz.hamiltonian,ansatz.nterms,ansatz.ep_cont=chop_to_real(commutator_list[op_idx],adapt=True)
            # Pre-transpile parametrized circuits
            expectation=ac.construct_and_transpile_circuits(ansatz=ansatz, optimization_level=3, ucc_operator_pool=new_operator_pool, device_name=device_name, simulator=simulator, save_info=False)

#            print('new_amplitudes:',new_amplitudes)
            residual_gradients_i[op_idx] = calculate_expectation(ansatz_params=new_amplitudes,expectation=expectation)
#            print('residual_gradients_i[op_idx]:',residual_gradients_i[op_idx])

        residual_gradients_i = np.abs(residual_gradients_i)
        residual_gradients_i_norm = np.linalg.norm(residual_gradients_i)
        residual_gradients_i_max = np.max(residual_gradients_i)
        residual_gradients_i_max_idx = np.argmax(residual_gradients_i)
        print("  Residual gradient norm: %10.6f max: %10.6f" %
              (residual_gradients_i_norm, residual_gradients_i_max))
        if (residual_gradients_i_norm < adapt_tol):
            print("Optimization completed because the norm f  the residual gradient is smaller than %.6e" % (adapt_tol))
            break

        # Select operator with the largest (abs value of) residual gradient
        print("  Select operator with index %d" %
              (residual_gradients_i_max_idx))

        new_operator_pool.append(
            ucc_operator_pool_qubitOp[residual_gradients_i_max_idx])

        # Perform VQE optimization using the new operator pool
        starting_amplitudes_i = np.array(new_amplitudes + [0])

        # Generate the initial parameters
        ansatz.num_params =len(new_operator_pool)
        ansatz.params=starting_amplitudes_i
        ansatz.hamiltonian,ansatz.nterms,ansatz.ep_cont=chop_to_real(hamiltonian_qubitOp)
#        ansatz.hamiltonian=hamiltonian_qubitOp
#        all_params = [ansatz.params]

        # Pre-transpile parametrized circuits
        expectation=ac.construct_and_transpile_circuits(ansatz=ansatz, optimization_level=3, ucc_operator_pool=new_operator_pool, device_name=device_name, simulator=simulator)

        result = scipy.optimize.minimize(calculate_expectation, ansatz.params, args=(expectation),
                                    method=optimize_method, options={"disp": True,"maxiter": epochs})

        opt_energy_i = result.fun
        opt_amplitudes_i = result.x

        new_energy = opt_energy_i
        new_amplitudes = opt_amplitudes_i.tolist()
        ansatz.params = opt_amplitudes_i.tolist()

#        print('ansatz.params:',ansatz.params)
        plot_list_cost.append([new_energy,ansatz.fci_e,new_energy-ansatz.fci_e])
 
        if abs(new_energy-ansatz.fci_e) < abs(ansatz.min_e-ansatz.fci_e):
            ansatz.min_e=new_energy
            sd.save(ansatz=ansatz, all_params_epochs=[new_amplitudes], plot_list_cost=plot_list_cost)
        else:
            sd.save(plot_list_cost=plot_list_cost)

    return new_energy