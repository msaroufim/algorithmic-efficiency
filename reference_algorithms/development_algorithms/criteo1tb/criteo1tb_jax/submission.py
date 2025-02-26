"""Training algorithm track submission functions for Criteo1TB DLRM-Small."""

import functools
from typing import Dict, Iterator, List, Tuple

from flax import jax_utils
import jax
from jax import lax
import jax.numpy as jnp
import optax

from algorithmic_efficiency import spec


def get_batch_size(workload_name):
  # Return the global batch size.
  del workload_name
  return 524_288 // 2


def create_learning_rate_fn(workload: spec.Workload,
                            hparams: spec.Hyperparameters):
  """Create learning rate schedule."""
  warmup_fn = optax.linear_schedule(
      init_value=0.,
      end_value=hparams.learning_rate,
      transition_steps=hparams.warmup_steps)
  cosine_fn = optax.cosine_decay_schedule(
      init_value=hparams.learning_rate,
      decay_steps=(workload.step_hint - hparams.warmup_steps))
  schedule_fn = optax.join_schedules(
      schedules=[warmup_fn, cosine_fn], boundaries=[hparams.warmup_steps])
  return schedule_fn


def init_optimizer_state(workload: spec.Workload,
                         model_params: spec.ParameterContainer,
                         model_state: spec.ModelAuxiliaryState,
                         hyperparameters: spec.Hyperparameters,
                         rng: spec.RandomState) -> spec.OptimizerState:
  del model_params
  del model_state
  del rng
  learning_rate_fn = create_learning_rate_fn(workload, hyperparameters)
  opt_init_fn, opt_update_fn = optax.adamw(
    learning_rate=learning_rate_fn,
    b1=hyperparameters.beta1,
    weight_decay=hyperparameters.weight_decay)
  params_zeros_like = jax.tree_map(lambda s: jnp.zeros(s.shape_tuple),
                                   workload.param_shapes)
  optimizer_state = opt_init_fn(params_zeros_like)
  return jax_utils.replicate(optimizer_state), opt_update_fn


@functools.partial(
    jax.pmap,
    axis_name='batch',
    in_axes=(None, None, 0, 0, 0, 0, 0),
    static_broadcasted_argnums=(0, 1))
def pmapped_train_step(workload,
                       opt_update_fn,
                       model_state,
                       optimizer_state,
                       current_param_container,
                       batch,
                       rng):

  def _loss_fn(params):
    """loss function used for training."""
    logits, new_model_state = workload.model_fn(
        params,
        batch,
        model_state,
        spec.ForwardPassMode.TRAIN,
        rng,
        update_batch_norm=False)
    loss_dict = workload.loss_fn(batch['targets'], logits)
    loss = loss_dict['summed'] / loss_dict['n_valid_examples']
    return loss, new_model_state

  grad_fn = jax.value_and_grad(_loss_fn, has_aux=True)
  (loss, new_model_state), grad = grad_fn(current_param_container)
  (loss, grad) = lax.pmean((loss, grad), axis_name='batch')
  grad_norm = jnp.sqrt(
      sum(jnp.sum(g**2) for g in jax.tree_util.tree_leaves(grad)))
  updates, new_optimizer_state = opt_update_fn(grad, optimizer_state,
                                               current_param_container)
  updated_params = optax.apply_updates(current_param_container, updates)
  return new_model_state, new_optimizer_state, updated_params, loss, grad_norm


def update_params(workload: spec.Workload,
                  current_param_container: spec.ParameterContainer,
                  current_params_types: spec.ParameterTypeTree,
                  model_state: spec.ModelAuxiliaryState,
                  hyperparameters: spec.Hyperparameters,
                  batch: Dict[str, spec.Tensor],
                  loss_type: spec.LossType,
                  optimizer_state: spec.OptimizerState,
                  eval_results: List[Tuple[int, float]],
                  global_step: int,
                  rng: spec.RandomState) -> spec.UpdateReturn:
  """Return (updated_optimizer_state, updated_params, updated_model_state)."""
  del current_params_types
  del loss_type
  del eval_results
  # del global_step

  optimizer_state, opt_update_fn = optimizer_state
  per_device_rngs = jax.random.split(rng, jax.local_device_count())
  new_model_state, new_optimizer_state, new_params, loss, grad_norm = pmapped_train_step(
      workload, opt_update_fn, model_state, optimizer_state,
      current_param_container, batch, per_device_rngs)
  if workload.metrics_logger is not None:
    workload.metrics_logger.append_scalar_metrics(
        {
            'loss': loss[0],
            'grad_norm': grad_norm[0],
        }, global_step)
  return (new_optimizer_state, opt_update_fn), new_params, new_model_state


def data_selection(workload: spec.Workload,
                   input_queue: Iterator[Dict[str, spec.Tensor]],
                   optimizer_state: spec.OptimizerState,
                   current_param_container: spec.ParameterContainer,
                   model_state: spec.ModelAuxiliaryState,
                   hyperparameters: spec.Hyperparameters,
                   global_step: int,
                   rng: spec.RandomState) -> Dict[str, spec.Tensor]:
  """Select data from the infinitely repeating, pre-shuffled input queue.

  Each element of the queue is a batch of training examples and labels.
  """
  del workload
  del optimizer_state
  del current_param_container
  del model_state
  del hyperparameters
  del global_step
  del rng
  return next(input_queue)
