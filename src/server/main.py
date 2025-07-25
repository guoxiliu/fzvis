#!/usr/bin/env python

from argparse import ArgumentParser
from flask import Flask, request, jsonify, send_file, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import json
import libpressio
import math
import numpy as np
import netCDF4 as nc
import os
from pathlib import Path
from pprint import pprint
import threading 
from openai import OpenAI

# CONSTANTS
DEFAULT_SYSTEM_CONFIG_PROMPT = "You are an expert in lossy compression with deep knowledge of scientific data. Please provide clear and concise answers to all questions. If you are not sure about the answer, please say so. Don't make up answers. Please always reply within 200 words if possible."
LLM_MODEL_NAME = "deepseek-ai/deepseek-r1-0528"

# Useful paths and create necessary folders for the backend
project_root = Path(__file__).parent.parent.parent
dist_dir = Path(__file__).parent.parent / "usr/libexec/fzvis/ui"
root_dir = Path.home() / ".fzvis"
upload_dir = root_dir / "uploads"
work_dir = root_dir / "data"
work_dir.mkdir(parents=True, exist_ok=True)
upload_dir.mkdir(parents=True, exist_ok=True)
metadata_file = upload_dir / "metadata.json"
saved_datasets = {}

# Create Large Language Model (LLM) client
# You can request a free API key from NVIDIA at
# https://build.nvidia.com/models
client = OpenAI(
    base_url = "https://integrate.api.nvidia.com/v1",
    api_key = os.environ.get("LLM_API_KEY", ""),
)
conversation_history = [{"role": "system", "content": DEFAULT_SYSTEM_CONFIG_PROMPT}]

app = Flask(__name__)
CORS(app)

# Get the file size in a human readable format
def get_human_readable_size(filepath): 
    if os.path.isfile(filepath):
        file_size = os.path.getsize(filepath)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if file_size < 1024.0:
                return f"{file_size:.2f} {unit}"
            file_size /= 1024.0


# Read input data from the file
def read_input_data(dataset):
    global input_data
    filepath = upload_dir / dataset["name"]
    width = int(dataset["width"])
    height = int(dataset["height"])
    depth = int(dataset["depth"])
    if dataset["precision"] == 'd': 
        input_data = np.fromfile(filepath, dtype=np.float64)
    elif dataset["precision"] == 'f': 
        input_data = np.fromfile(filepath, dtype=np.float32)

    if len(input_data) > depth*height*width: 
        input_data = input_data[len(input_data)- depth*height*width:]
    
    input_data = input_data.reshape(depth, height, width)
    # input_data = np.nan_to_num(input_data, nan=0)


# Read NetCDF file
def read_netcdf_file(filename, variable, slice_params=None):
    filepath = upload_dir / filename
    with nc.Dataset(filepath) as dataset:
        if variable == "metadata":
            return {
                var_name: {
                    "shape": dataset.variables[var_name].shape,
                    "dimensions": dataset.variables[var_name].dimensions,
                    "dtype": str(dataset.variables[var_name].dtype),
                }
                for var_name in dataset.variables
            }
        elif variable == "all":
            var_data = {}
            for var_name in dataset.variables:
                var = dataset.variables[var_name]
                data = np.nan_to_num(var[:], nan=0)
                var_data[var_name] = data.flatten().tolist()
            return var_data
        else:
            # update the input_data
            global input_data
            var_data = dataset.variables[variable][:]
            print(variable,"dimensions:", dataset.variables[variable].dimensions)
            # if slicing needs to be applied
            if slice_params:
                slices = []
                for dim_slice in slice_params:
                    sl = slice(
                        dim_slice.get("start", 0),
                        dim_slice.get("end", None),
                        dim_slice.get("step", 1)
                    )
                    slices.append(sl)
                input_data = var_data[tuple(slices)]
                print("nan locations:", np.where(np.isnan(input_data)))
            else: 
                input_data = var_data
            return input_data


# Save metadata to the disk
def save_metadata_to_file(filekey, metadata):
    saved_datasets[filekey] = metadata
    with open(metadata_file, 'w') as f:
        json.dump(saved_datasets, f, indent=4)


# Route to get the list of uploaded datasets
@app.route("/listDatasets", methods=["GET", "POST"])
def get_uploaded_datasets():
    return jsonify({"datasets" : saved_datasets}), 200


# Route to send the file back to the front end
@app.route("/download", methods=["GET", "POST"])
def send_data_file(): 
    filename = request.args.get("filename")
    filetype = request.args.get("filetype")

    # Check if the file exists first
    filepath = upload_dir / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    
    # Handle different file types
    if filetype == "plain":
        return send_file(filepath, as_attachment=False)
    elif filetype == "netcdf":
        variable = request.args.get("variable")
        slices = request.args.get("slices")
        slice_params = json.loads(slices) if slices else None
        var_data = read_netcdf_file(filename, variable, slice_params)
        # Send the array as bytes
        return Response(var_data.tobytes(), mimetype="application/octet-stream")


# Route to handle file upload
@app.route("/upload", methods=["POST"])
def upload_file():
    try:
        result = {}
        dataset_metadata = {}
        read_data_file = False
        filepath = ""
        # Uploading a new dataset 
        if "file" in request.files: 
            file = request.files["file"]
            if file.filename == "":
                return jsonify({"error" : "No file selected"}), 400

            # Save file
            filename = file.filename
            filepath = upload_dir / filename
            file.save(filepath)

            # Use the filename as the key for now as we don't allow two duplicate files
            dataset_metadata["name"] = filename
            dataset_metadata["size"] = get_human_readable_size(filepath)
            read_data_file = True
            
        # Updating an existing dataset
        elif request.form.get("filename"):
            filename = request.form.get("filename")
            dataset_metadata = saved_datasets[filename]

        # Handle different file types
        file_type = request.form.get("type")
        dataset_metadata["type"] = file_type

        if file_type == "netcdf":
            if read_data_file:
                dataset = nc.Dataset(filepath)
                variable_keys = dataset.variables.keys()
                dataset_metadata["vars"] = read_netcdf_file(dataset_metadata["name"], "metadata")
        
        elif file_type == "plain":
            dataset_metadata["width"] = request.form.get("width")
            dataset_metadata["height"] = request.form.get("height")
            dataset_metadata["depth"] = request.form.get("depth")
            dataset_metadata["precision"] = request.form.get("precision")
            if read_data_file:
                threading.Thread(target=read_input_data, args=(dataset_metadata,)).start()

        # Save metadata to a json file
        saved_datasets[filename] = dataset_metadata
        result["dataset"] = dataset_metadata
        with open(metadata_file, 'w') as f:
            json.dump(saved_datasets, f, indent=4)
        return jsonify(result), 200
        
    except Exception as e:
        print("Error in upload_file():", e)
        return jsonify({"error" : str(e)}), 500


# Route to handle datasets update
@app.route("/updateDatasets", methods=["POST"])
def update_datasets():
    try:
        # Update the currently working dataset
        if request.form.get("currentDataset"):
            currentDataset = json.loads(request.form["currentDataset"])
            # print("currentDataset:", currentDataset)
            if currentDataset["type"] == "plain":
                threading.Thread(target=read_input_data, args=(currentDataset,)).start()

        # Remove the files if deleted datasets are provided
        if request.form.get("deletedDatasets"):
            deletedDatasets = json.loads(request.form["deletedDatasets"])
            print("deletedDatasets: ", deletedDatasets)
            for d in deletedDatasets:
                filepath = upload_dir / saved_datasets[d].get("name")
                if os.path.isfile(filepath):
                    os.remove(filepath)
                saved_datasets.pop(d)
            # Update the metadata file
            with open(metadata_file, 'w') as f:
                json.dump(saved_datasets, f, indent=4)
        return jsonify({"datasets" : saved_datasets}), 200

    except Exception as e:
        print(e)
        return jsonify({"error" : str(e)}), 500

# Route to get the list of uploaded datasets
@app.route("/allCompressors", methods=["GET"])
def get_available_compressors():
    try: 
        c = libpressio.PressioCompressor("pressio", name="pressio")
        compressors = c.get_configuration()["pressio"]["pressio:compressor"]
        compressors.remove("pressio")
        return jsonify({"compressors" : compressors}), 200
    except Exception as e:
        print("Error in get_available_compressors():", e)
        return jsonify({"error": str(e)}), 500

@app.route("/indexlist", methods=["POST"])
def indexlist():
    try:
        option = int(request.form.get("get_options"))
        # Run all submitted compressors and compare the results
        if(option == 0):
            global input_data
            def replace_unsupported_values(obj):
                if isinstance(obj, dict):
                    return {k: replace_unsupported_values(v) for k, v in obj.items()}
                elif obj == math.inf:
                    return "Infinity"
                elif obj == -math.inf:
                    return "-Infinity"
                elif obj is None:
                    return 'null'
                elif isinstance(obj, float) and math.isnan(obj):
                    return 'null'
                else:
                    return obj
            def comparing_compressor(arguments):
                global input_data
                # print("arguments: ", arguments)

                configs = {
                    "compressor_id": arguments["compressor_id"],
                    "early_config": {
                        "pressio:metric": "composite",
                        "composite:plugins": arguments["early_config"].get("composite:plugins", []),
                    },
                    "compressor_config": arguments["compressor_config"],
                }
                def run_compressor(args):
                    global input_data
                    # Check if the configuration is valid
                    compressor = libpressio.PressioCompressor.from_config({
                        "compressor_id": args["compressor_id"],
                        "early_config": args["early_config"],
                        "compressor_config": args["compressor_config"],
                    })
                    decomp_data = input_data.copy()
                    comp_data = compressor.encode(input_data)
                    decomp_data = compressor.decode(comp_data, decomp_data)
                    metrics = compressor.get_metrics()
                    metrics1 = replace_unsupported_values(metrics)
                    
                    return {
                        "compressor_id": args["compressor_id"],
                        "metrics": metrics1,
                        "decp_data": decomp_data.flatten().tolist(),
                    }
                result = run_compressor(configs)
                return result
            
            if input_data is None:
                return jsonify({"error": "No input dataset at backend!"}), 400
            configurations = json.loads(request.form.get("configurations"))
            # pprint(configurations)
            result = {}
            for name, config in configurations.items():
                print("Running compressor: ", name)
                if(config["compressor_id"] != ''):
                    output = comparing_compressor(config)
                    result[name] = output
                    # print("original data non-zero values:", np.count_nonzero(input_data))
                    # print("decompressed data non-zero values:", np.count_nonzero(output["decp_data"]))
            return result, 200
        
        elif option == 1:
            # pprint(libpressio.PressioCompressor("roibin", {"roibin:roi": "sz3"}).get_configuration())
            compressor_id = request.form["compressor_id"]
            compressor = libpressio.PressioCompressor(compressor_id)
            options = compressor.get_configuration()
            doc = compressor.get_documentation()
            highlevel = []
            if "pressio:highlevel" in options:
                highlevel = options["pressio:highlevel"]
            # print("options:", json.dumps(options, indent=4, sort_keys=True))
            module_slots = {k: options[k] for k in options if k.startswith(compressor_id)}
            # print("highlevel:", highlevel)
            # print("options:", module_slots)
            
            return jsonify({"doc": doc, "highlevel" : highlevel, "options" : module_slots}), 200 
    
    except Exception as e:
        print("Error in indexlist():", e)
        return jsonify({"error": str(e)}), 500

# Route to get AI response
@app.route("/chat", methods=["POST"])
def get_ai_response(): 
    try:
        message = request.form.get("message")
        if not message:
            return jsonify({"error": "No input provided"}), 400
        
        # Create and return streaming completion using conversation history
        conversation_history.append({"role": "user", "content": message})
        response = client.chat.completions.create(
            model="deepseek-ai/deepseek-r1-0528",
            messages=conversation_history,
            temperature=0.6,
            top_p=0.7,
            max_tokens=4096,
            stream=True,
        )

        # Stream the response back to the client
        def generate():
            full_response = ""
            for chunk in response:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    reasoning = delta.reasoning_content or ""
                    content = delta.content or ""

                    if content:
                        full_response += content
                    
                    payload = {
                        "reasoning_content": reasoning,
                        "content": content
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
            conversation_history.append({"role": "assistant", "content": full_response})
            yield "data: [DONE]\n\n"
        
        return Response(
            stream_with_context(generate()), 
            mimetype="text/event-stream")

    except Exception as e:
        print("Error in get_ai_response():", e)
        return jsonify({"error": str(e)}), 500


# Catch-all route to serve the Vue frontend's index.html
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_frontend(path):
    print(f"Requested path: {path}")
    print(f"dist_dir: {dist_dir}")
    full_path = os.path.join(dist_dir, path)
    print(f"Full path: {full_path}")
    if os.path.isfile(full_path):
        return send_from_directory(dist_dir, path)
    else:
        return send_from_directory(dist_dir, 'index.html')


# Main entry of the server program
if __name__ == '__main__':

    # Initialize necessary data
    input_data = None
    width = -1
    height = -1
    depth = -1

    # Parsing command line arguments
    parser = ArgumentParser(description="enter your HOST/POST.", usage="path/to/main.py [OPTIONAL ARGUMENTS] <HOST> <PORT> <configfile>")
    parser.add_argument('--HOST', nargs='?', help='HOST_address', default="0.0.0.0")
    parser.add_argument('--PORT', nargs='?', help='PORT_address', default="5003")
    parser.add_argument('--configfile', nargs='?', help='your_config_file', default=None)
    input = parser.parse_args()

    if not any(vars(input).values()):
        parser.print_help()
    api_host = input.HOST
    api_port = input.PORT
    config = {
        "API_HOST": api_host,
        "API_PORT": api_port
    }
    with open((project_root / "serverConfig.json"), 'w') as json_file:
        json.dump(config, json_file, indent=4)
    
    # Read uploaded datasets from the metadata file
    if metadata_file.exists():
        with open(metadata_file, 'r') as f:
            saved_datasets = json.load(f)

    app.run(host=api_host, port=api_port, debug=True)
