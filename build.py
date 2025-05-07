import os
import time
import json
import hashlib
from pathlib import Path

import boto3
import requests

SETUP_SCRIPT_FILENAME = "setup.sh"
CLOUD_INIT_FILE = "cloud-init.yaml"


def download_setup_script(setup_script_url, setup_script_file):
    with requests.get(setup_script_url, stream=True) as response:
        response.raise_for_status()

        with open(setup_script_file, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)


def get_setup_script_hash(setup_script_file):
    sha256_hash = hashlib.sha256()

    with open(setup_script_file, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256_hash.update(chunk)

    return sha256_hash.hexdigest()


def get_base_image_id(region):
    if region == "us-east-1":
        return "ami-0f88e80871fd81e91"
    elif region == "us-east-2":
        return "ami-058a8a5ab36292159"
    elif region == "us-west-1":
        return "ami-04fc83311a8d478df"
    elif region == "us-west-2":
        return "ami-07b0c09aab6e66ee9"
    elif region == "ap-northeast-1":
        return "ami-0c2da9ee6644f16e5"
    else:
        raise Exception(f"This region is currently unsupported")


def launch_vm_instance(ec2_client, base_image_id, setup_script_url, setup_script_hash):
    with open(f"{Path(__file__).resolve().parent}/{CLOUD_INIT_FILE}", "r") as file:
        cloud_init_script = file.read()
        cloud_init_script = cloud_init_script.replace(
            "DUMMY_SETUP_SCRIPT_URL", setup_script_url)
        cloud_init_script = cloud_init_script.replace(
            "DUMMY_SETUP_SCRIPT_HASH", setup_script_hash)

    try:
        response = ec2_client.run_instances(
            ImageId=base_image_id,
            InstanceType="t3.medium",
            MinCount=1,
            MaxCount=1,
            UserData=cloud_init_script
        )

        return response["Instances"][0]
    except Exception as e:
        raise Exception(f"Failed to launch the VM instance: {e}")


def wait_for_vm_ready(ec2_client, instance_id, timeout=90):
    try:
        start_time = time.time()
        while time.time() - start_time < timeout:
            # check instance state
            state_resp = ec2_client.describe_instance_status(
                InstanceIds=[instance_id],
                IncludeAllInstances=True
            )

            if state_resp["InstanceStatuses"][0]["InstanceState"]["Name"] != "running":
                time.sleep(5)
                continue

            # check console output
            output_resp = ec2_client.get_console_output(
                InstanceId=instance_id, Latest=True)
            output = output_resp["Output"]

            if "OSD fail" in output:
                raise RuntimeError(
                    f"Error occurred during cloud-init execution")
            if "ODS complete" in output:
                return

            time.sleep(5)

        raise RuntimeError(f"Timeout while waiting for the VM to become ready")
    except Exception as e:
        raise Exception(f"Failed while waiting for the VM to be ready: {e}")


def create_image_from_vm(ec2_client, instance_id):
    try:
        response = ec2_client.create_image(
            InstanceId=instance_id,
            Name=f"{instance_id}-ami",
            NoReboot=True
        )

        return response['ImageId']
    except Exception as e:
        raise Exception(f"Failed to create an image from the VM: {e}")


def validate_vm_state(ec2_client, instance_id, launch_info):
    try:
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        instance_info = response["Reservations"][0]["Instances"][0]

        # check instance launch time
        if not launch_info["LaunchTime"] == instance_info["LaunchTime"]:
            raise RuntimeError(
                "Instance launch time has changed since initial boot")

        # check replace root volume task
        replace_tasks = ec2_client.describe_replace_root_volume_tasks(
            Filters=[{"Name": "instance-id", "Values": [instance_id]}])

        if replace_tasks["ReplaceRootVolumeTasks"]:
            raise RuntimeError(
                "Root volume has been replaced after initial boot")
    except Exception as e:
        raise Exception(f"Error validating VM state: {e}")


def terminate_vm_instance(ec2_client, instance_id):
    try:
        ec2_client.terminate_instances(InstanceIds=[instance_id])
    except Exception as e:
        print(f"Failed to terminate instance {instance_id}: {e}")


def main():
    # retrieve parameters
    params = {
        "awsRegion": os.environ.get("AWS_REGION"),
        "setupScriptURL": os.environ.get("SETUP_SECRIPT_URL"),
        "setupScriptHash": os.environ.get("SETUP_SECRIPT_HASH"),
        "awsAccessKey": os.environ.get("AWS_ACCESS_KEY"),
        "awsAccessSecret": os.environ.get("AWS_ACCESS_SECRET"),
        "outputFolder": os.environ.get("OUTPUT_FOLDER")
    }

    build_success = False
    launch_info = None
    boto3_session = boto3.Session(
        aws_access_key_id=params["awsAccessKey"],
        aws_secret_access_key=params["awsAccessSecret"],
    )
    ec2_client = boto3_session.client(
        "ec2", region_name=params["awsRegion"])

    try:
        # download the setup script and verify its hash
        print("Checking setup script")
        setup_script_path = Path(
            params["outputFolder"]) / SETUP_SCRIPT_FILENAME
        download_setup_script(params["setupScriptURL"], setup_script_path)
        setup_script_hash = get_setup_script_hash(setup_script_path)

        if params["setupScriptHash"] != setup_script_hash:
            raise Exception(f"setup script hash mismatch")

        # launch instance
        print("Launching VM instance")
        launch_info = launch_vm_instance(ec2_client, get_base_image_id(
            params["awsRegion"]), params["setupScriptURL"], params["setupScriptHash"])

        # wait for instance
        print("Waiting for the VM instance to become ready")
        wait_for_vm_ready(ec2_client, launch_info["InstanceId"])

        # create image from vm
        print("Creating image")
        image_id = create_image_from_vm(ec2_client, launch_info["InstanceId"])

        # validate instance status
        print("Validating VM instance state")
        validate_vm_state(ec2_client, launch_info["InstanceId"], launch_info)

        # generate build statement
        print("Generating build statement")
        statement = {
            "region": params["awsRegion"],
            "imageID": image_id,
            "setupScriptHash": setup_script_hash
        }
        with open(Path(params["outputFolder"]) / f"{image_id}.json", "w") as json_file:
            json.dump(statement, json_file)

        build_success = True
    except Exception as e:
        print(e)
    finally:
        print("Cleaning resources")
        terminate_vm_instance(ec2_client, launch_info["InstanceId"])

    if not build_success:
        exit(1)


if __name__ == "__main__":
    main()
