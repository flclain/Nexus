import logging
import os
import warnings
from typing import List, Optional, Union

import hydra
import pytorch_lightning as pl
from nuplan.planning.script.builders.scenario_building_builder import \
    build_scenario_builder
from nuplan.planning.script.builders.simulation_callback_builder import \
    build_simulation_callbacks
from nuplan.planning.script.utils import (run_runners, set_default_path,
                                          set_up_common_builder)
from nuplan.planning.simulation.planner.abstract_planner import AbstractPlanner
from nuplan_extent.planning.script.builders.horizon_simulation_builder import \
    build_horizon_simulations
from omegaconf import DictConfig, OmegaConf

warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# If set, use the env. variable to overwrite the default dataset and
# experiment paths
set_default_path()

# If set, use the env. variable to overwrite the Hydra config
CONFIG_PATH = os.getenv('NUPLAN_HYDRA_CONFIG_PATH', 'config')

CONFIG_NAME = 'horizon_simulation'

def run_simulation(
        cfg: DictConfig,
        planners: Optional[
            Union[AbstractPlanner, List[AbstractPlanner]]] = None) -> None:
    """
    Execute all available challenges simultaneously on the same scenario. Helper function for main to allow planner to
    be specified via config or directly passed as argument.
    :param cfg: Configuration that is used to run the experiment.
        Already contains the changes merged from the experiment's config to default config.
    :param planners: Pre-built planner(s) to run in simulation. Can either be a single planner or list of planners
    """
    # Fix random seed
    pl.seed_everything(cfg.seed, workers=True)

    profiler_name = 'building_simulation'
    common_builder = set_up_common_builder(
        cfg=cfg, profiler_name=profiler_name)

    # Build scenario builder
    scenario_builder = build_scenario_builder(cfg=cfg)

    # Run scenario simulations
    callbacks = build_simulation_callbacks(
        cfg=cfg, output_dir=common_builder.output_dir)

    # Remove planner from config to make sure run_simulation does not receive
    # multiple planner specifications.
    if planners and 'planner' in cfg.keys():
        logger.info(
            'Using pre-instantiated planner. Ignoring planner in config')
        OmegaConf.set_struct(cfg, False)
        cfg.pop('planner')
        OmegaConf.set_struct(cfg, True)

    # Construct simulations
    if isinstance(planners, AbstractPlanner):
        planners = [planners]

    runners = build_horizon_simulations(
        cfg=cfg,
        callbacks=callbacks,
        scenario_builder=scenario_builder,
        worker=common_builder.worker,
        pre_built_planners=planners,
    )

    if common_builder.profiler:
        # Stop simulation construction profiling
        common_builder.profiler.save_profiler(profiler_name)

    logger.info('Running simulation...')
    run_runners(
        runners=runners,
        common_builder=common_builder,
        cfg=cfg,
        profiler_name='running_simulation')
    logger.info('Finished running simulation!')


@hydra.main(config_path=CONFIG_PATH, config_name=CONFIG_NAME)
def main(cfg: DictConfig) -> None:
    """
    Execute all available challenges simultaneously on the same scenario. Calls run_simulation to allow planner to
    be specified via config or directly passed as argument.
    :param cfg: Configuration that is used to run the experiment.
        Already contains the changes merged from the experiment's config to default config.
    """
    assert cfg.simulation_log_main_path is None, 'Simulation_log_main_path must not be set when running simulation.'

    # Execute simulation with preconfigured planner(s).
    run_simulation(cfg=cfg)


if __name__ == '__main__':
    main()
