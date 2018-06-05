""" Callbacks that are executed before or after retrieving API data. """

import logging
import os

from dateutil import parser
from retriever.callback_helpers import normalize_java, get_added_lines
from util.exceptions import IllegalConfigurationError

# get root logger
logger = logging.getLogger('api-retriever_logger')


#########################
# pre_request_callbacks #
#########################

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
    See entity configuration: gh_repo_path_codeblock___commits
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
    Filter entities where the normalized code block matches one of the file diffs (patches).
    See entity configuration: gh_repo_path_codeblock_commit___files
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


def filter_patches_with_line(entity):
    """
    Filter entities where the line containing a link to Stack Overflow matches one of the file diffs (patches).
    See entity configuration: gh_repo_path_line_url_commit___files
    :param entity: An entity with output parameter "files" and input parameter "line".
    :return: True if the line is found in the commit diff, False otherwise.
    """
    # search for match of line in commit diff
    for file in entity.output_parameters["files"]:
        if file["filename"] == entity.input_parameters["path"]:
            patch = file.get("patch", None)
            if patch:
                patch_lines = patch.split('\n')
                for line in patch_lines:
                    if line.startswith("+") and line[1:].strip() == entity.input_parameters["line"].strip():
                        # add commit diff to output
                        entity.output_parameters["commit_diff"] = patch
                        # remove files from output
                        entity.output_parameters.pop('files')
                        return True

    return False


def set_destination_path(entity):
    """
    Add destination path for raw content to output parameters of an entity.
    See entity configuration: gh_repo_path_branch___file
    :param entity:
    """
    if entity.output_parameters[entity.configuration.raw_parameter] is None:
        return
    repo_name = entity.input_parameters["repo_name"].split("/")
    user = repo_name[0]
    repo = repo_name[1]
    path = entity.input_parameters["path"].replace("/", " ")
    # add destination path to output
    entity.output_parameters["destination"] = os.path.join(user, repo, path)


def extract_email_from_commits(entity):
    """
    Extract email from commit data, add to output, remove commits.
    See entity configuration: gh_user_repo___commit_email
    :param entity: An entity having "commits" with "author_email" as output parameter.
    :return: True if email address has been extracted, False otherwise.
    """
    if entity.output_parameters["commits"]:
        for commit in entity.output_parameters["commits"]:
            author_email = commit["author_email"]
            if author_email and "@" in author_email:
                entity.output_parameters["author_email"] = author_email
                break

        entity.output_parameters.pop('commits')
        if "author_email" in entity.output_parameters.keys():
            return True

    return False
