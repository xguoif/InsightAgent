# prometheus
This agent collects data from prometheus and sends it to Insightfinder.
## Installing the Agent

### Required Dependencies:
1. Python 3.x 
1. Pip3

###### Installation Steps:
1. Download the prometheus.tar.gz package
1. Copy the agent package to the machine that will be running the agent
1. Extract the package
1. Navigate to the extracted location 
1. Configure venv and python dependencies
1. Configure agent settings under `conf.d/`
1. Test the agent
1. Add agent to the cron

The final steps are described in more detail below. 

###### Configure venv and python dependencies:
The configure_python.sh script sets up a virtual python environment and installs all required libraries for running the agent. 

```bash
./setup/configure_python.sh
```

###### Agent configuration:
The config.ini file contains all of the configuration settings needed to connect to the Prometheus instance and to stream the data to InsightFinder.

The password for the Prometheus user will need to be obfuscated using the ifobfuscate.py script.  It will prompt you for the password and provide the value to add to the configuration file. 

```
python ./ifobfuscate.py 
```

The configure_python.sh script will generate a config.ini file for you; however, if you need to create a new one, you can simply copy the config.ini.template file over the config.ini file to start over. 

Populate all of the necessary fields in the config.ini file with the relevant data.  More details about each field can be found in the comments of the config.ini file and the Config Variables below. 

###### Test the agent:
Once you have finished configuring the config.ini file, you can test the agent to validate the settings. 

This will connect to the Prometheus instance, but it will not send any data to InsightFinder. This allows you to verify that you are getting data from Prometheus and that there are no failing exceptions in the agent configuration.

User `-p` to define max processes, use `--timeout` to define max timeout.

```bash
./setup/test_agent.sh
```

###### Add agent to the cron:
For the agent to run continuously, it will need to be added as a cron job. 

The install_cron.sh script will add a cron file to run the agent on a regular schedule.

```bash
# Display the cron entry without adding it 
./setup/install_cron.sh --display

# Add the cron entry, once you are ready to start streaming
sudo ./setup/install_cron.sh --create
```

###### Pausing or stopping the agent:
Once the cron is running, you can either pause the agent by commenting out the relevant line in the cron file or stop the agent by removing the cron file. 

### Config Variables
* **`prometheus_uri`**: URI for Prometheus API as `scheme://host:port`. Defaults to `http://localhost:9090`
* `metrics`: Metrics to query for. Multiple fields are separated by `;`. If none specified, all metrics returned from `/api/v1/label/__names__/values` will be used.
* `metrics_whitelist`: metrics_whitelist is a regex string used to define which metrics will be filtered.
* `metrics_to_ignore`: Comma-delimited metrics to not report. Defaults to `ALERTS,ALERTS_FOR_STATE`.
* `query_label_selector`: Label selector to use when querying for metrics, ie `{namespace="monitoring"}`.
* `query_with_function`: Label function to calculate the metric's value, now only support functions: `increase`. Example: `query_with_function = increase`.
* `metrics_whitelist_with_function`: This field is a regex string used to define which metrics will be calculate with `query_with_function`. Example: `metrics_whitelist_with_function = total_mem_.*`.
* `metrics_name_field`: This field is used to get metric's name from response data field. Multiple fields are separated by commas. EX: `__name__, job`, the `metric name` =  `{__name__}_{job}`.  If none specified, agent will use the metric name from config var `metrics`.
* `his_time_range`: History data time range, Example: 2020-04-14 00:00:00,2020-04-15 00:00:00. If this option is set, the agent will query metric values by time range.
* **`data_format`**: The format of the data to parse: RAW, RAWTAIL, CSV, CSVTAIL, XLS, XLSX, JSON, JSONTAIL, AVRO, or XML. \*TAIL formats keep track of the current file being read & the position in the file.
* `timestamp_format`: Format of the timestamp, in python [arrow](https://arrow.readthedocs.io/en/latest/#supported-tokens). If the timestamp is in Unix epoch, this can be set to `epoch`. If the timestamp is split over multiple fields, curlies can be used to indicate formatting, ie: `YYYY-MM-DD HH:mm:ss ZZ`; alternatively, if the timestamp can be in one of multiple fields, a priority list of field names can be given: `timestamp1,timestamp2`.
* `timezone`: Timezone of the timestamp data stored in/returned by the DB. Note that if timezone information is not included in the data returned by the DB, then this field has to be specified. 
* `timestamp_field`: Field name for the timestamp. Default is `timestamp`.
* `target_timestamp_timezone`: Timezone of the timestamp data to be sent and stored in InsightFinder. Default value is UTC. Only if you wish to store data with a time zone other than UTC, this field should be specified to be the desired time zone.
* `component_field`: Field name for the component name.
* `instance_field`: Field name for the instance name. If not set or the field is not found, the instance name is the hostname of the machine the agent is installed on. This can also use a priority list, field names can be given: `instance1,instance2`.
* `instance_whitelist`: This field is a regex string used to define which instances will be filtered.
* `device_field`: Field name for the device/container for containerized projects. This can also use a priority list, field names can be given: `device1,device2`.
* `data_fields`: Comma-delimited list of field names to use as data fields. If not set, all fields will be reported. Each data field can either be a field name (`name`) or a labeled field (`<name>::<value>` or `<name>::==<value>`), where `<name>` and `<value>` can be raw strings (`fieldname::fieldvalue`), curly or complex formatted (`link!!ref=json&auth!!name::=={val} - {ue}`), or a combination. If `::==` is used as the separator, `<value>` is treated as a mathematical expression that can be evaluated with `eval()`.
* `thread_pool`: Number of thread to used in the pool, default is 20.
* `agent_http_proxy`: HTTP proxy used to connect to the agent.
* `agent_https_proxy`: As above, but HTTPS.
* **`user_name`**: User name in InsightFinder
* **`license_key`**: License Key from your Account Profile in the InsightFinder UI. 
* `token`: Token from your Account Profile in the InsightFinder UI. 
* **`project_name`**: Name of the project created in the InsightFinder UI. 
* **`project_type`**: Type of the project - one of `metric, metricreplay, log, logreplay, incident, incidentreplay, alert, alertreplay, deployment, deploymentreplay`.
* **`sampling_interval`**: How frequently (in Minutes) data is collected. Should match the interval used in project settings.
* **`run_interval`**: How frequently (in Minutes) the agent is ran. Should match the interval used in cron.
* `chunk_size_kb`: Size of chunks (in KB) to send to InsightFinder. Default is `2048`.
* `if_url`: URL for InsightFinder. Default is `https://app.insightfinder.com`.
* `if_http_proxy`: HTTP proxy used to connect to InsightFinder.
* `if_https_proxy`: As above, but HTTPS.


