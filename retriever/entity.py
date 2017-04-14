""" Data model for the API retriever. """

import codecs
import csv
import json
import logging
import os
import time
from inspect import signature

import requests

from random import randint
from jsmin import jsmin
from _socket import gaierror
from requests.packages.urllib3.exceptions import MaxRetryError
from requests.packages.urllib3.exceptions import NewConnectionError
from retriever import callbacks
from util.data_processing import get_value_from_nested_dictionary
from util.exceptions import IllegalArgumentError, IllegalConfigurationError
from util.uri_template import URITemplate

# get root logger
logger = logging.getLogger('api-retriever_logger')


class EntityConfiguration(object):
    """
    An API entity configuration specifies:
        * the input parameters for an entity
        * if and how existing information about an entity should be validated (-> validation parameters)
        * which information should be retrieved about an entity (-> output parameters)
        * how this information should be extract from the API response (-> response callback).
    """

    def __init__(self, config):
        """
        Initialize an API entity configuration from a config dictionary.
        :param config: a dictionary with all required parameters
        """

        try:
            # name of configured entities
            self.name = config["name"]
            # list with parameters that identify the entity (correspond to columns in the input CSV)
            self.input_parameters = config["input_parameters"]
            # dictionary with mapping of parameter names to values in the response
            self.output_parameter_mapping = config["output_parameter_mapping"]
            # uri templates to retrieve information about the entity (may include API key)
            self.uri_template = URITemplate(config["uri_template"])
            # load pre_request_callbacks to validate and/or process the parameters before the request to the API is made
            self.pre_request_callbacks = []
            for callback in config["pre_request_callbacks"]:
                try:
                    self.pre_request_callbacks.append(getattr(callbacks, callback))
                except AttributeError:
                    raise IllegalConfigurationError("Parsing configuration file failed: Callback "
                                                    + callback + " not found.")
            # load post_request_callbacks to extract and/or process output parameters from a JSON API response
            self.post_request_callbacks = []
            for callback in config["post_request_callbacks"]:
                try:
                    self.post_request_callbacks.append(getattr(callbacks, callback))
                except AttributeError:
                    raise IllegalConfigurationError("Parsing configuration file failed: Callback "
                                                    + callback + " not found.")
            # API key to include in the uri_template
            self.api_key = config["api_key"]
            # configure if duplicate values in the input files should be ignored.
            self.ignore_duplicates = config["ignore_duplicates"]
            # configure the randomized delay interval (ms) between two API requests (trying to prevent getting blocked)
            self.delay_min = config["delay"][0]
            self.delay_max = config["delay"][1]

        except KeyError as e:
            raise IllegalConfigurationError("Reading configuration failed: Parameter " + str(e) + " not found.")

    @classmethod
    def create_from_json(cls, json_config_file):
        """
        Create API entity configuration from a JSON file.
        :param json_config_file: path to the JSON file with the configuration
        """

        # read config file
        with open(json_config_file) as config_file:
            # remove comments from JSON file (which we allow, but the standard does not)
            stripped_json = jsmin(config_file.read())
            # parse JSON file
            config = json.loads(stripped_json)

        return EntityConfiguration(config)


class Entity(object):
    """
    Class representing one API entity for which information should be retrieved over an API.
    """

    def __init__(self, configuration, input_parameter_values):
        """
        To initialize an entity, a corresponding entity configuration together with values for the input parameter(s)
        and (optional) validation parameter(s) are needed.
        :param configuration: an object of class EntityConfiguration
        :param input_parameter_values: values for the input parameters defined in the configuration
        """

        # corresponding entity configuration
        self.configuration = configuration
        # parameters needed to identify entity read from CSV
        self.input_parameters = dict.fromkeys(configuration.input_parameters)
        # parameters that should be retrieved using the API
        self.output_parameters = dict.fromkeys(configuration.output_parameter_mapping.keys())

        # set values for input parameters
        for parameter in configuration.input_parameters:
            if parameter not in input_parameter_values:
                raise IllegalArgumentError("Illegal input parameter: " + parameter)
            self.input_parameters[parameter] = input_parameter_values[parameter]

        # get uri for this entity from uri template in configuration
        uri_variable_values = {
            **self.input_parameters,
            "api_key": self.configuration.api_key
        }
        self.uri = self.configuration.uri_template.replace_variables(uri_variable_values)

    def equals(self, other_entity):
        """
        Function to compare two entities according to their input parameters (needed to remove duplicates).
        :param other_entity: the entity to compare self to
        :return: True if entities have the same input parameters, False otherwise
        """

        # compare input parameters
        for parameter in self.input_parameters.keys():
            try:
                if not self.input_parameters[parameter] == other_entity.input_parameters[parameter]:
                    return False
            except KeyError:
                # parameter does not exist in other entity
                return False
        return True

    def __str__(self):
        return str(self.input_parameters)

    def retrieve_data(self, session):
        """
        Retrieve information about entity using an existing session.
        :param session: requests session to use for data retrieval
        :return: True if data about entity has been retrieved, False otherwise
        """

        try:
            logger.info("Retrieving data for entity " + str(self) + "...")

            # execute pre_request_callbacks
            for callback in self.configuration.pre_request_callbacks:
                parameter_count = len(signature(callback).parameters)
                if parameter_count == 1:
                    callback(self)
                else:
                    raise IllegalArgumentError("Invalid callback: " + str(callback))

            # reduce request frequency as configured
            delay = randint(self.configuration.delay_min,
                            self.configuration.delay_max)  # delay between requests in milliseconds
            time.sleep(delay / 1000)  # sleep for delay ms to not get blocked by Airbnb

            # retrieve data
            response = session.get(self.uri)

            if not response.ok:
                logger.error("Error " + str(response.status_code) + ": Could not retrieve data for entity " + str(self)
                             + ". Response: " + str(response.content))
                return False
            else:
                logger.info("Successfully retrieved data for entity " + str(self) + ".")

                # deserialize JSON string
                json_response = json.loads(response.text)

                # extract parameters according to parameter mapping
                self._extract_output_parameters(json_response)

                # execute post_request_callbacks
                for callback in self.configuration.post_request_callbacks:
                    parameter_count = len(signature(callback).parameters)
                    if parameter_count == 1:
                        callback(self)
                    elif parameter_count == 2:
                        callback(self, json_response)
                    else:
                        raise IllegalArgumentError("Invalid callback: " + str(callback))

                return True

        except (gaierror,
                ConnectionError,
                MaxRetryError,
                NewConnectionError):
            logger.error("An error occurred while retrieving data for entity  " + str(self))

    def _extract_output_parameters(self, json_response):
        """
        Extracts and saves all parameters defined in the output parameter mapping.
        :param json_response: the API response as JSON object
        """

        # extract data for all parameters according to access path defined in the entity configuration
        for parameter in self.configuration.output_parameter_mapping.keys():
            mapping = self.configuration.output_parameter_mapping[parameter]

            if mapping[0] == "*":  # mapping starts with asterisk -> root of JSON response is list (JSON array)

                if len(mapping) == 1:  # if no further arguments are provided, save complete list
                    for element in json_response:
                        self.output_parameters[parameter] = element
                    return

                if len(mapping) == 2:  # second element is mapping for list element parameters
                    list_element_mapping = mapping[1]
                    extracted_list_elements = []  # extracted parameters from list elements are stored here

                    for element in json_response:
                        list_element_parameters = dict.fromkeys(list_element_mapping.keys())

                        for list_element_parameter in list_element_parameters.keys():
                            access_path = list_element_mapping[list_element_parameter]
                            try:
                                parameter_value = get_value_from_nested_dictionary(element, access_path)
                            except KeyError:
                                logger.error("Could not retrieve data for parameter " + parameter
                                             + " of entity " + str(self))
                                parameter_value = None
                            list_element_parameters[list_element_parameter] = parameter_value

                        extracted_list_elements.append(list_element_parameters)

                    self.output_parameters[parameter] = extracted_list_elements
                    return

            elif mapping[0] == ".":  # mapping starts with dot -> root of JSON response is dictionary (JSON object)

                access_path = mapping[1] # second element is access path for dictionary

                try:
                    parameter_value = get_value_from_nested_dictionary(json_response, access_path)
                except KeyError:
                    logger.error("Could not retrieve data for parameter " + parameter
                                 + " of entity " + str(self))
                    parameter_value = None

                self.output_parameters[parameter] = parameter_value

            else:
                raise IllegalConfigurationError("First element of output parameter mapping must be '.' (dictionary) or"
                                                "'*' (list).")


class EntityList(object):
    """ List of API entities. """

    def __init__(self, configuration):
        """
        To initialize the list, an entity configuration is needed.
        :param configuration: object of class EntityConfiguration
        """
        self.configuration = configuration
        # list to stores entity objects
        self.list = []
        # session for data retrieval
        self.session = requests.Session()

    def read_from_csv(self, input_file, delimiter):
        """
        Read entity ID and (optionally) validation parameters from a CSV file (header required).
        :param input_file: path to the CSV file
        :param delimiter: column delimiter in CSV file (typically ',')
        """

        # read CSV as UTF-8 encoded file (see also http://stackoverflow.com/a/844443)
        with codecs.open(input_file, encoding='utf8') as fp:
            logger.info("Reading entities from " + input_file + "...")
            reader = csv.reader(fp, delimiter=delimiter)
            header = next(reader, None)
            input_parameter_indices = dict.fromkeys(self.configuration.input_parameters)  # column indices in CSV
            input_parameter_values = dict.fromkeys(self.configuration.input_parameters)  # values for input parameters

            if not header:
                raise IllegalArgumentError("Missing header in CSV file.")

            # number of columns must equal number of input parameters
            if not len(header) == len(input_parameter_indices):
                raise IllegalArgumentError("Wrong number of columns in CSV file.")

            # check if columns and parameters match, store indices
            for index in range(len(header)):
                if not header[index] in input_parameter_indices.keys():
                    raise IllegalArgumentError("Unknown column name in CSV file: " + header[index])
                input_parameter_indices[header[index]] = index

            # read CSV file
            for row in reader:
                if row:
                    # read parameters
                    for parameter in input_parameter_indices.keys():
                        value = row[input_parameter_indices[parameter]]
                        if value:
                            input_parameter_values[parameter] = value
                        else:
                            raise IllegalArgumentError("No value for parameter " + parameter)

                    # create entity from values in row
                    new_entity = Entity(self.configuration, input_parameter_values)

                    # check if entity already exists (if ignore_duplicates is configured)
                    if self.configuration.ignore_duplicates:
                        entity_exists = False
                        for entity in self.list:
                            if new_entity.equals(entity):
                                entity_exists = True
                        if not entity_exists:
                            # add new entity to list
                            self.list.append(new_entity)
                    else:
                        # add new entity to list
                        self.list.append(new_entity)
                else:
                    raise IllegalArgumentError("Wrong CSV format.")

        logger.info(str(len(self.list)) + " entities have been imported.")

    def retrieve_data(self):
        """
        Retrieve data for all entities in the list.
        """
        count = 0
        for entity in self.list:
            if entity.retrieve_data(self.session):
                count += 1
        logger.info("Data for " + str(count) + " entities has been retrieved.")

    def write_to_csv(self, output_dir, delimiter):
        """
        Write entities together with retrieved data to a CSV file.
        :param output_dir: target directory for generated CSV file 
        :param delimiter: column delimiter in CSV file (typically ',')
        """

        if len(self.list) == 0:
            logger.info("Nothing to write...")

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        file_path = os.path.join(
            output_dir,
            '{0}.csv'.format(self.configuration.name)
        )

        # write entity list to UTF8-encoded CSV file (see also http://stackoverflow.com/a/844443)
        with codecs.open(file_path, 'w', encoding='utf8') as fp:
            logger.info('Writing entities to ' + file_path + '...')
            writer = csv.writer(fp, delimiter=delimiter)

            # write header of CSV file
            column_names = self.configuration.input_parameters \
                + list(self.configuration.output_parameter_mapping.keys())
            writer.writerow(column_names)

            for entity in self.list:
                try:
                    row = []
                    for column_name in column_names:
                        if column_name in entity.input_parameters.keys():
                            row.append(entity.input_parameters[column_name])
                        elif column_name in entity.output_parameters.keys():
                            row.append(entity.output_parameters[column_name])
                    if len(row) == len(column_names):
                        writer.writerow(row)
                    else:
                        raise IllegalArgumentError(str(len(row) - len(column_names)) + " parameters are missing for"
                                                                                       "entity " + str(entity))

                except UnicodeEncodeError:
                    logger.error("Encoding error while writing data for entity: " + str(entity))

            logger.info(str(len(self.list)) + ' entities have been exported.')
