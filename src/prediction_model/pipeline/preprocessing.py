import sys
import os
import math
import pickle
import csv
import pandas as pd
import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from sklearn.ensemble import RandomForestClassifier
from io import StringIO

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from prediction_model.config import config

def is_csv_empty(file_path):
    with open(file_path, 'r') as file:
        header = file.readline().strip()
        for line in file:
            if line.strip():  # If we find a non-empty line
                return False
        return True

def filter_features(element):
    return {key: element[key] for key in config.FEATURES_AFTER_CONTRACT}

def find_leading_column_index(header, target_column=config.LEADING_COLUMN):
    columns = header.split(',')
    if target_column in columns:
        return columns.index(target_column)
    else:
        raise ValueError(f"'{target_column}' column not found in the header.")
    
def convert_dict_to_string(element):
    return ','.join(str(element.get(col, '')) for col in config.FEATURES)

def dict_to_csv_row(element, headers=None):

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    if headers:
        writer.writerow(element)
    else:
        writer.writerow(element)
    
    return output.getvalue().strip()

class FillTotalCharges(beam.DoFn):
    def __init__(self, default_value=2279.0):
        self.default_value = default_value

    def process(self, element):
        total_charges = element.get(config.FEATURE_TO_MODIFY, str(self.default_value))
        if total_charges is None or total_charges == '' or isinstance(total_charges, float) and math.isnan(total_charges):
            total_charges = str(self.default_value)

        total_charges = total_charges.replace(' ', str(self.default_value))
        
        try:
            element['TotalCharges'] = float(total_charges)
        except ValueError:
            element['TotalCharges'] = self.default_value
        
        yield element

class FilterInvalidContract(beam.DoFn):
    def __init__(self):
        # Initialize a counter for invalid rows
        self.invalid_count = beam.metrics.Metrics.counter(self.__class__, 'invalid_contract_rows')
    
    def process(self, element):
        contract_value = element.get('Contract')
        valid_contracts = {'One year', 'Two year', 'Month-to-month'}
        
        if (contract_value not in valid_contracts):
            # TODO: COUNTER!! 
            pass
        else:
            # Yield only valid rows
            yield element

class OHEContract(beam.DoFn):
    def __init__(self):
        self.contract_mapping = {
            'One year': {'One year': 1, 'Two year': 0, 'Month-to-month': 0},
            'Two year': {'One year': 0, 'Two year': 1, 'Month-to-month': 0},
            'Month-to-month': {'One year': 0, 'Two year': 0, 'Month-to-month': 1}
            }
        
    def process(self, element):
        element.update(self.contract_mapping.get(element['Contract'], {'One year': 0, 'Two year': 0, 'Month-to-month': 0}))
        # TODO: ERROR HANDLING
        element.pop('Contract', None)
        yield element

class HandleContract(beam.DoFn):
    def process(self, element):
        if pd.isna(element['Contract']).any():
            # TODO:: ALERT!!
            raise ValueError("Null value found in 'Contract' feature")
        return [element]

class FillPhoneService(beam.DoFn):
    def process(self, element):
        # Check if the 'PhoneService' value is None or NaN and replace it with 'No'
        phone_service_value = element.get('PhoneService')
        
        if phone_service_value is None or \
           (isinstance(phone_service_value, float) and math.isnan(phone_service_value)):
            # TODO: MONITORRR
            element['PhoneService'] = 'No'
        
        yield element

class FillTenureWithMean(beam.DoFn):
    def __init__(self):
        self.tenure_mean = pd.read_csv(config.ORIG_DATA_PATH)['tenure'].mean()
    
    def process(self, element):
        tenure_value = element.get('tenure')
        if tenure_value is None or \
           (isinstance(tenure_value, float) and math.isnan(tenure_value)):
            # TODO: COUNTER HERE
            element['tenure'] = float(self.tenure_mean)
        elif tenure_value == '':
            element['tenure'] = 0.0
        else:
            element['tenure'] = float(element['tenure'])
        
        yield element

class MapPhoneService(beam.DoFn):
    def process(self, element):
        element['PhoneService'] = 1 if element['PhoneService'].lower() == 'yes' else 0
        yield element

def run_pipeline(input_file, output_file, db_out_suffix):
    options = PipelineOptions()
    p = beam.Pipeline(options=options)
    
    if is_csv_empty(file_path=input_file):
        # TODO: MONITOR LOG
        print(f"{input_file} is empty or only contains a header.")
        return False
    else:
        print(f"{input_file} contains data.")

    with open(input_file, 'r') as f:
        header = f.readline().strip()
        customer_id_index = find_leading_column_index(header)

    processed_data = (p
     | 'Read CSV' >> beam.io.ReadFromText(input_file, skip_header_lines=1)
     | 'To Dict' >> beam.Map(lambda x: dict(zip(config.COLUMN_NAMES, x.split(',')[customer_id_index:])))
     | 'Filter Features' >> beam.Map(filter_features)
     | 'Filter Invalid Contracts' >> beam.ParDo(FilterInvalidContract())
     | 'OHE Contract' >> beam.ParDo(OHEContract())
     | 'Fill TotalCharges' >> beam.ParDo(FillTotalCharges())
     | 'Fill PhoneService' >> beam.ParDo(FillPhoneService())
     | 'Fill Tenure' >> beam.ParDo(FillTenureWithMean())
     | 'Map PhoneService' >> beam.ParDo(MapPhoneService())
     | 'Convert Dict to String' >> beam.Map(convert_dict_to_string)
    )

    header_str = ','.join(config.FEATURES)
    output_data = (
        (p | 'Create Header' >> beam.Create([header_str]), processed_data)
        | 'Reattach Header' >> beam.Flatten()
    )
    output_data | 'Write Output' >> beam.io.WriteToText(output_file, file_name_suffix=db_out_suffix, shard_name_template='')
    
    result = p.run()
    result.wait_until_finish()

    return True

if __name__ == '__main__':
    input_file = os.path.join(config.DATA_PATH, config.TEST_FILE_FIVE)
    output_file = os.path.join(config.DATA_PATH, 'outputs/processed_output')
    db_out_suffix = '.csv'
    shards = 1

    pipline_success = run_pipeline(input_file, output_file, db_out_suffix)

    if pipline_success:
        with open(os.path.join(config.SAVE_MODEL_PATH, config.MODEL_NAME), 'rb') as f:
            rf_model = pickle.load(f)
        
        # TODO: ASSERT 
        if isinstance(rf_model, RandomForestClassifier):
            print("The model was loaded successfully and is a RandomForestClassifier.")

        # 2. Check some of the model's attributes
        ## ASSERT TODO: 
        print("Number of trees in the forest:", rf_model.n_estimators)
        print("Features considered in the first tree:", rf_model.estimators_[0].n_features_in_)

        print(rf_model.predict(pd.read_csv(output_file + db_out_suffix)))
        # TODO: CHECK THAT THE RESULTS MATCH THE ONES FROM THE DS