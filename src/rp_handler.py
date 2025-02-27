import runpod
from runpod.serverless.utils.rp_upload import get_boto_client
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
import uuid
import logging

logger = logging.getLogger("runpod comfyui handler")
FMT = "%(filename)-20s:%(lineno)-4d %(asctime)s %(message)s"
logging.basicConfig(level=logging.DEBUG, format=FMT, handlers=[logging.StreamHandler()])


# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = 50
# Maximum number of API check attempts
COMFY_API_AVAILABLE_MAX_RETRIES = 500
# Time to wait between poll attempts in milliseconds
COMFY_POLLING_INTERVAL_MS = 250
# Maximum number of poll attempts
COMFY_POLLING_MAX_RETRIES = 500
# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"


def check_server(url, retries=50, delay=500):
    """
    Check if a server is reachable via HTTP GET request

    Args:
    - url (str): The URL to check
    - retries (int, optional): The number of times to attempt connecting to the server. Default is 50
    - delay (int, optional): The time in milliseconds to wait between retries. Default is 500

    Returns:
    bool: True if the server is reachable within the given number of retries, otherwise False
    """

    for i in range(retries):
        try:
            response = requests.get(url)

            # If the response status code is 200, the server is up and running
            if response.status_code == 200:
                print(f"runpod-worker-comfy - API is reachable")
                return True
        except requests.RequestException as e:
            # If an exception occurs, the server may not be ready
            pass

        # Wait for the specified delay before retrying
        time.sleep(delay / 1000)

    print(
        f"runpod-worker-comfy - Failed to connect to server at {url} after {retries} attempts."
    )
    return False


def queue_prompt(prompt):
    """
    Queue a prompt to be processed by ComfyUI

    Args:
        prompt (dict): A dictionary containing the prompt to be processed

    Returns:
        dict: The JSON response from ComfyUI after processing the prompt
    """
    data = json.dumps(prompt).encode("utf-8")
    req = urllib.request.Request(f"http://{COMFY_HOST}/prompt", data=data)
    return json.loads(urllib.request.urlopen(req).read())


def get_history(prompt_id):
    """
    Retrieve the history of a given prompt using its ID

    Args:
        prompt_id (str): The ID of the prompt whose history is to be retrieved

    Returns:
        dict: The history of the prompt, containing all the processing steps and results
    """
    with urllib.request.urlopen(f"http://{COMFY_HOST}/history/{prompt_id}") as response:
        return json.loads(response.read())


def base64_encode(img_path):
    """
    Returns base64 encoded image.

    Args:
        img_path (str): The path to the image

    Returns:
        str: The base64 encoded image
    """
    with open(img_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
        return f"data:image/png;base64,{encoded_string}"



# ---------------------------------------------------------------------------- #
#                                 Upload Image                                 #
# ---------------------------------------------------------------------------- #
def upload_image(job_id, image_location, result_index=0, results_list=None, bucket=None):  # pragma: no cover
    '''
    Upload a single file to bucket storage.
    '''
    image_name = str(uuid.uuid4())[:8]
    boto_client, _ = get_boto_client()
    file_extension = os.path.splitext(image_location)[1]
    content_type = "image/" + file_extension.lstrip(".")

    with open(image_location, "rb") as input_file:
        output = input_file.read()

    if boto_client is None:
        # Save the output to a file
        print("No bucket endpoint set, saving to disk folder 'simulated_uploaded'")
        print("If this is a live endpoint, please reference the following:")
        print("https://github.com/runpod/runpod-python/blob/main/docs/serverless/utils/rp_upload.md")  # pylint: disable=line-too-long

        os.makedirs("simulated_uploaded", exist_ok=True)
        sim_upload_location = f"simulated_uploaded/{image_name}{file_extension}"

        with open(sim_upload_location, "wb") as file_output:
            file_output.write(output)

        if results_list is not None:
            results_list[result_index] = sim_upload_location

        return sim_upload_location

    if not bucket:
        bucket = os.getenv("BUCKET_NAME")

    if not bucket:
        bucket = time.strftime('%m-%y')
    boto_client.put_object(
        Bucket=f'{bucket}',
        Key=f'{job_id}/{image_name}{file_extension}',
        Body=output,
        ContentType=content_type
    )

    presigned_url = boto_client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': f'{bucket}',
            'Key': f'{job_id}/{image_name}{file_extension}'
        }, ExpiresIn=604800)

    if results_list is not None:
        results_list[result_index] = presigned_url

    return presigned_url


def process_output_images(outputs, job_id, output_path = None):
    """
    This function takes the "outputs" from image generation and the job ID,
    then determines the correct way to return the image, either as a direct URL
    to an AWS S3 bucket or as a base64 encoded string, depending on the
    environment configuration.

    Args:
        outputs (dict): A dictionary containing the outputs from image generation,
                        typically includes node IDs and their respective output data.
        job_id (str): The unique identifier for the job.

    Returns:
        dict: A dictionary with the status ('success' or 'error') and the message,
              which is either the URL to the image in the AWS S3 bucket or a base64
              encoded string of the image. In case of error, the message details the issue.

    The function works as follows:
    - It first determines the output path for the images from an environment variable,
      defaulting to "/comfyui/output" if not set.
    - It then iterates through the outputs to find the filenames of the generated images.
    - After confirming the existence of the image in the output folder, it checks if the
      AWS S3 bucket is configured via the BUCKET_ENDPOINT_URL environment variable.
    - If AWS S3 is configured, it uploads the image to the bucket and returns the URL.
    - If AWS S3 is not configured, it encodes the image in base64 and returns the string.
    - If the image file does not exist in the output folder, it returns an error status
      with a message indicating the missing image file.
    """

    # The path where ComfyUI stores the generated images
    logger.info(f"\n\nComfy image generation finished. ")
    print(f"Comfy Outputs: {outputs}")


    COMFY_OUTPUT_PATH = os.environ.get('COMFY_OUTPUT_PATH', "/comfyui/output")
    output_images = []

    for node_id, node_output in outputs.items():
        if "images" in node_output:
            for image in node_output["images"]:
                output_images.append(image["filename"])

    print(f"runpod-worker-comfy - image generation is done")

    # expected image output folder
    local_image_paths = [f"{COMFY_OUTPUT_PATH}/{output_image}" for output_image in output_images]
    images = []

    for local_image_path in local_image_paths:
        # The image is in the output folder
        if os.path.exists(local_image_path):
            print("runpod-worker-comfy - the image exists in the output folder")

            if os.environ.get('BUCKET_ENDPOINT_URL', False):
                # URL to image in AWS S3
                image = upload_image(output_path, local_image_path)
                print(f"image saved in aws bucket: {image}")
                images.append(image)
            else:
                # base64 image
                image = base64_encode(local_image_path)
                images.append(image)

        else:
            print("runpod-worker-comfy - the image does not exist in the output folder")
            return {
                "status": "error",
                "message": f"the image does not exist in the specified output folder: {local_image_path}",
            }
    
    return {
        "status": "success",
        "message": images
    }
    

def handler(job):
    """
    The main function that handles a job of generating an image.

    This function validates the input, sends a prompt to ComfyUI for processing,
    polls ComfyUI for result, and retrieves generated images.

    Args:
        job (dict): A dictionary containing job details and input parameters.

    Returns:
        dict: A dictionary containing either an error message or a success status with generated images.
    """
    job_input = job["input"]

    logger.info(f"""

    

    Start handler ...
    job: {job}


        
    """)

    if "bucket_creds" in job_input:
        bucket_creds = job_input.pop("bucket_creds")
        os.environ["BUCKET_ENDPOINT_URL"] = bucket_creds.get("endpointUrl")
        os.environ["BUCKET_ACCESS_KEY_ID"] = bucket_creds.get("accessId")
        os.environ["BUCKET_SECRET_ACCESS_KEY"] = bucket_creds.get("accessSecret")
        os.environ["BUCKET_NAME"] = bucket_creds.get("bucketName")

    polling_max_retries = job_input.get("polling_max_retries", COMFY_POLLING_MAX_RETRIES)
    output_path = job_input.get("output_path")

    print(f"Polling max retries: {polling_max_retries}\nOutput path: {output_path}")

    # Make sure that the ComfyUI API is available
    check_server(
        f"http://{COMFY_HOST}",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    )

    # Validate input
    if job_input is None:
        return {"error": "Please provide the 'prompt'"}

    # Is JSON?
    if isinstance(job_input, dict):
        if "comfy_input" in job_input:
            prompt = job_input["comfy_input"]
        else:
            print("comfyui not in job_input, job_input:", job_input)
            prompt = job_input
    # Is String?
    elif isinstance(job_input, str):
        try:
            prompt = json.loads(job_input)
            if "comfy_input" in prompt:
                prompt  = prompt["comfy_input"]
            else:
                print("comfyui not in prompt, prompt:", prompt)
        except json.JSONDecodeError:
            return {"error": "Invalid JSON format in 'prompt'"}
    else:
        return {"error": f"'prompt' must be a JSON object or a JSON-encoded string, job_input: {job_input}"}

    print("Straing Queue ...")
    print(f"Prompt: {prompt}")

    # Queue the prompt
    try:
        queued_prompt = queue_prompt(prompt)

        print(f"Queued Prompt Return: {queued_prompt}")
        prompt_id = queued_prompt["prompt_id"]
        print(f"runpod-worker-comfy - queued prompt with ID {prompt_id}")
    except Exception as e:
        return {"error": f"Error queuing prompt: {str(e)}"}

    # Poll for completion
    print(f"\n\nrunpod-worker-comfy - wait until image generation is complete")
    retries = 0
    try:
        while retries < polling_max_retries:
            history = get_history(prompt_id)

            # Exit the loop if we have found the history
            if prompt_id in history and history[prompt_id].get("outputs"):
                break
            else:
                # Wait before trying again
                time.sleep(COMFY_POLLING_INTERVAL_MS / 1000)
                retries += 1
        else:
            return {"error": "Max retries reached while waiting for image generation"}
    except Exception as e:
        return {"error": f"Error waiting for image generation: {str(e)}"}

    logger.info("Runpod Handler function finished")
    # Get the generated image and return it as URL in an AWS bucket or as base64
    return process_output_images(history[prompt_id].get("outputs"), job["id"], output_path=output_path)


# Start the handler only if this script is run directly
if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
