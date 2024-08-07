import asyncio
import json
import logging
import multiprocessing as mp
import os
import pathlib
from functools import partial

import pandas as pd
from datasets import load_dataset

from evaluation.biocoder.biocoder_env_box import BiocoderData, BiocoderSSHBox
from evaluation.utils.shared import (
    EvalMetadata,
    codeact_user_response,
    make_metadata,
    monologue_user_response,
    prepare_dataset,
    run_evaluation,
)
from opendevin.controller.agent import Agent
from opendevin.controller.state.state import State
from opendevin.core.config import LLMConfig, config, get_llm_config_arg, parse_arguments
from opendevin.core.logger import get_console_handler
from opendevin.core.logger import opendevin_logger as logger
from opendevin.core.main import run_agent_controller
from opendevin.events.serialization.event import event_to_dict
from opendevin.llm.llm import LLM


def cleanup():
    print('Cleaning up child processes...')
    for process in mp.active_children():
        print(f'Terminating child process: {process.name}')
        process.terminate()
        process.join()


AGENT_CLS_TO_FAKE_USER_RESPONSE_FN = {
    'CodeActAgent': partial(
        codeact_user_response, encapsulate_solution=True, try_parse=None
    ),
    'MonologueAgent': monologue_user_response,
}

AGENT_CLS_TO_INST_SUFFIX = {
    'CodeActAgent': 'When you think you have fixed the issue through code changes, please run the following command: <execute_bash> exit </execute_bash>.\n'
}


def get_test_result(instance, sandbox, workspace_dir_name):
    test_result = {'result': {}, 'metadata': {}}
    try:
        code = sandbox.get_changed_code(include_signature=True)
        sandbox.copy_changed_code()
        test_result['metadata']['1_copy_change_success'] = True
        test_result['metadata']['1_copy_change_code'] = code
    except Exception:
        logger.error('Error fetching changed code for this instance')
        test_result['metadata']['1_copy_change_success'] = False
        test_result['metadata']['1_copy_change_code'] = None

    exit_code, output = sandbox.execute_and_check(
        'cd /testing',
        'Failed to cd /testing',
    )
    logger.info(f'cd $REPO_PATH: {output}')

    exit_code, output = sandbox.execute_and_check(
        'whoami',
        'Failed to run whoami',
    )
    logger.info(f'whoami: {output}')

    exit_code, output = sandbox.execute(
        '/home/devin/mambaforge/bin/mamba run -n test python3 /testing/start_test_opendevin.py'
    )
    logger.info(f'$TEST_CMD:\n{output}')

    exit_code, output = sandbox.execute_and_check(
        'cat /testing_files/results_biocoder.json', 'Failed to read the result file'
    )
    if exit_code == 0:
        test_result['metadata']['2_run_test_success'] = True
        test_result['metadata']['2_run_test_result'] = str(output)
    else:
        test_result['metadata']['2_run_test_success'] = False
        test_result['metadata']['2_run_test_result'] = str(output)
    json_obj = json.loads(output)
    test_result['result'] = json_obj['result']

    return test_result


def process_instance(
    instance: pd.Series,
    metadata: EvalMetadata,
    reset_logger: bool = True,
):
    # Create the agent
    agent = Agent.get_cls(metadata.agent_class)(llm=LLM(llm_config=metadata.llm_config))
    instance = BiocoderData(**instance)
    print(instance)
    workspace_dir_name = (
        f'{instance.repository}__{instance.test_case_id[:10]}__{os.getpid()}'.replace(
            '/', '__'
        )
    )
    workspace_mount_path = os.path.join(config.workspace_base, workspace_dir_name)
    # create process-specific workspace dir
    # if `not skip_workspace_mount` - we will create a workspace directory for EACH process
    # so that different agent don't interfere with each other.
    workspace_mount_path = os.path.join(workspace_mount_path, str(os.getpid()))
    pathlib.Path(workspace_mount_path).mkdir(parents=True, exist_ok=True)

    # Setup the logger properly, so you can run multi-processing to parallize the evaluation
    if reset_logger:
        # Set up logger
        log_file = os.path.join(
            metadata.eval_output_dir, 'logs', f'instance_{instance.test_case_id}.log'
        )
        # Remove all existing handlers from logger
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        # add back the console handler to print ONE line
        logger.addHandler(get_console_handler())
        logger.info(
            f'Starting evaluation for instance {instance.test_case_id}.\nHint: run "tail -f {log_file}" to see live logs in a seperate shell'
        )
        # Remove all existing handlers from logger
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        logger.addHandler(file_handler)

    logger.info(f'Process-specific workspace mounted at {workspace_mount_path}')

    # NOTE: this is something special we do for SWE-Bench due to the reason described in the previous section
    # You can omit this if you don't need to setup specialized sandbox
    workspace_dir_name = f'{instance.repository}__{instance.test_case_id[:10]}'.replace(
        '/', '__'
    )
    sandbox = BiocoderSSHBox.get_box_for_instance(
        instance,
        workspace_dir_name,
        skip_workspace_mount=False,
        workspace_mount_path=workspace_mount_path,
        sandbox_plugins=agent.sandbox_plugins,
    )

    sandbox.remove_code()

    # Prepare instruction
    instruction = (
        f'Please complete the function "{instance.signature}" in the file /workspace/{instance.repository.split("/")[1]}/{instance.filePath}.\n'
        f'The environment has been set up for you to start working. You may assume all necessary tools are installed.\n'
        f'To complete the task, you must directly modify the file and fill in the function, keeping in mind that the function signature is on line {instance.lineStart-1}\n\n'
        f'The function should do the following:\n'
        f'{instance.promptSummaryOnly}\n\n'
    )

    instruction += (
        'IMPORTANT: You should ONLY interact with the environment provided to you AND NEVER ASK FOR HUMAN HELP.\n'
        'You should NOT modify any other files other than the file intended. This means that you should NOT write any test cases.\n'
        'You may need context from other files in the repository to complete this task.'
        'Do NOT add any import statements or change anything else other than the writing the function body.\n'
        'You do not need to run the code to check if it works. \n'
        'Make sure to include proper formatting in Java and Python, including correct braces and/or indentation.\n'
    )
    # NOTE: You can actually set slightly different instruction for different agents
    instruction += AGENT_CLS_TO_INST_SUFFIX[agent.__class__.__name__]

    # use a session id for concurrent evaluation
    sid = instance.test_case_id.replace('/', '__')

    # Here's how you can run the agent (similar to the `main` function) and get the final task state
    state: State | None = asyncio.run(
        run_agent_controller(
            agent,
            instruction,
            fake_user_response_fn=AGENT_CLS_TO_FAKE_USER_RESPONSE_FN[
                agent.__class__.__name__
            ],
            sandbox=sandbox,
            sid=sid,
        )
    )

    test_result = get_test_result(instance, sandbox, workspace_dir_name)

    if state is None:
        raise ValueError('State should not be None.')
    metrics = state.metrics.get() if state.metrics else None

    # Save the output
    output = {
        'test_case_id': instance.test_case_id,
        'biocoder_instance': instance.to_dict(),
        'instruction': instruction,
        'generated': test_result['metadata']['1_copy_change_code'],
        'metadata': metadata,
        'history': [
            (event_to_dict(action), event_to_dict(obs)) for action, obs in state.history
        ],
        'metrics': metrics,
        'error': state.last_error if state and state.last_error else None,
        'test_result': test_result,
    }

    # Close the sandbox
    sandbox.close()
    return output


if __name__ == '__main__':
    id_column = 'test_case_id'
    args = parse_arguments()
    dataset = load_dataset('lilbillbiscuit/biocoder_public')
    biocoder_tests = dataset['test'].to_pandas()
    llm_config = get_llm_config_arg(args.llm_config) if args.llm_config else LLMConfig()
    metadata = make_metadata(
        llm_config,
        args.dataset_name,
        args.agent_cls,
        args.max_iterations,
        args.eval_note,
        args.eval_output_dir,
    )
    output_file = os.path.join(metadata.eval_output_dir, 'output.jsonl')
    instances = prepare_dataset(dataset, output_file, args.eval_n_limit, id_column)
    agent = Agent.get_cls(metadata.agent_class)(llm=LLM(llm_config))
    run_evaluation(
        instances,
        metadata,
        output_file,
        args.eval_num_workers,
        process_instance,
        id_column,
    )
