GREEN=$(tput setaf 2)
NORMAL=$(tput sgr0)

printf "${GREEN}Installing TFX workshop${NORMAL}\n\n"

printf "${GREEN}Installing pendulum to avoid problem with tzlocal${NORMAL}\n"
pip install pendulum==2.1.2

printf "${GREEN}Installing TFX${NORMAL}\n"
pip install pyarrow==5.0.0 apache_beam==2.38.0 tfx==1.9.1

printf "${GREEN}Installing required packages for tft${NORMAL}\n"
pip install tensorflow-text==2.9.0 tensorflow-decision-forests==0.2.7 struct2tensor==0.45.0

# Airflow
# Set this to avoid the GPL version; no functionality difference either way
printf "${GREEN}Preparing environment for Airflow${NORMAL}\n"
export SLUGIFY_USES_TEXT_UNIDECODE=yes
printf "${GREEN}Installing Airflow${NORMAL}\n"
pip install -q apache-airflow==2.3.4 Flask Werkzeug

# Resolve dependency conflicts
pip install email-validator==2.0.0
pip install sqlalchemy==2.0

printf "${GREEN}Initializing Airflow database${NORMAL}\n"
airflow db init

# Adjust configuration
printf "${GREEN}Adjusting Airflow config${NORMAL}\n"
sed -i'.orig' 's/dag_dir_list_interval = 300/dag_dir_list_interval = 1/g' ~/airflow/airflow.cfg
sed -i'.orig' 's/job_heartbeat_sec = 5/job_heartbeat_sec = 1/g' ~/airflow/airflow.cfg
sed -i'.orig' 's/scheduler_heartbeat_sec = 5/scheduler_heartbeat_sec = 1/g' ~/airflow/airflow.cfg
sed -i'.orig' 's/dag_default_view = tree/dag_default_view = graph/g' ~/airflow/airflow.cfg
sed -i'.orig' 's/load_examples = True/load_examples = False/g' ~/airflow/airflow.cfg
sed -i'.orig' 's/max_threads = 2/max_threads = 1/g' ~/airflow/airflow.cfg

printf "${GREEN}Refreshing Airflow to pick up new config${NORMAL}\n"
airflow db reset --yes
airflow db init

# Copy Dags to ~/airflow/dags
mkdir -p ~/airflow/dags
cp dags/taxi_pipeline.py ~/airflow/dags/
cp dags/taxi_utils.py ~/airflow/dags/

# Copy data to ~/airflow/data
cp -R data ~/airflow

printf "\n${GREEN}TFX workshop installed${NORMAL}\n"
