""" Callbacks that are executed before or after retrieving API data. """

import logging

from dateutil import parser
from retriever.callback_helpers import normalize_java, get_added_lines
from util.exceptions import IllegalConfigurationError

# get root logger
logger = logging.getLogger('api-retriever_logger')


#########################
# pre_request_callbacks #
#########################

# Entity configuration:
def validate_code_block_normalization(entity):
    """
    Validate if normalize_java produces the same normalized string as imported from the CSV file.
    See entity configuration: gh_repo_commits_files_code_blocks
    :param entity: An entity to validate.
    :return: None
    """
    try:
        code_block = str(entity.input_parameters["code_block"])
        code_block_normalized = str(entity.input_parameters["code_block_normalized"])

        if normalize_java(code_block) == code_block_normalized:
            logger.info("Normalization successfully validated for entity " + str(entity))
        else:
            logger.error("Validation of normalization failed for entity " + str(entity))

    except KeyError as e:
        raise IllegalConfigurationError("Input parameter missing: " + str(e))


##########################
# post_request_callbacks #
##########################

def sort_commits(entity):
    """
    Sort the retrieved commits according to their commit date from old to new.
    See entity configuration: gh_repo_path_commits
    :param entity: An entity having "commits" as output parameter.
    :return: None
    """
    if entity.output_parameters["commits"]:
        # parse commit date strings (ISO 8601) into a python datetime object (see http://stackoverflow.com/a/3908349)
        for commit in entity.output_parameters["commits"]:
            commit["commit_date"] = parser.parse(commit["commit_date"])

        # sort commits (oldest commits first)
        entity.output_parameters["commits"] = sorted(entity.output_parameters["commits"], key=lambda c: c["commit_date"])

        # convert commit dates back to string representation
        for commit in entity.output_parameters["commits"]:
            commit["commit_date"] = str(commit["commit_date"])


def filter_patches_with_code_block(entity):
    """
    Filter entities where the normalized code block matches on of the file diffs (patches). 
    See entity configuration: gh_repo_commits_files_code_blocks
    :param entity: An entity with output parameter "files" and input parameter "code_block_normalized".
    :return: True if code block matches commit diff, False otherwise.
    """
    # search for match of code_block in commit diff
    for file in entity.output_parameters["files"]:
        if file["filename"] == entity.input_parameters["path"]:
            patch = file.get("patch", None)
            if patch:
                patch_normalized = normalize_java(get_added_lines(patch))
                if entity.input_parameters["code_block_normalized"] in patch_normalized:
                    # add commit diff to output
                    entity.output_parameters["commit_diff"] = patch
                    entity.output_parameters["commit_diff_normalized"] = patch_normalized
                    # remove files from output
                    entity.output_parameters.pop('files')
                    return True

    return False
