import os
import re
import shutil
import logging
import itertools

from docker.utils.json_stream import json_stream
from docker.errors import BuildError


class Generator:

    def __init__(self, client, script_name, script_dir, platform, image, keep_build, extra_options):
        super(Generator, self).__init__()
        self.logger = logging.getLogger(self.__class__.__name__)

        self.platform = platform
        self.image = image

        # run flags
        self.finished = False
        self.canceled = False

        # relative paths for working dir and dist dir, works both on host and inside containers
        self.working_dir = os.path.join("./build/", self.platform)
        self.dist_dir = os.path.join("./dist/", self.platform)

        # create dirs on host
        os.makedirs(os.path.join(script_dir, self.working_dir), exist_ok=True)
        os.makedirs(os.path.join(script_dir, self.dist_dir), exist_ok=True)

        # create command with relative paths, will be executed inside containers
        self.command = "\"pyinstaller \'{}\' --onefile --distpath {} --workpath {} --specpath {} {}\"".format(
            script_name,
            self.dist_dir,
            self.working_dir,
            self.working_dir,
            extra_options)

        self.container = None
        self.client = client
        self.keep_build = keep_build
        self.script_dir = script_dir

    def start(self):
        self.logger.info("Starting Docker image build.")
        try:
            build_path = os.path.join(os.path.dirname(__file__), "./Docker/")
            resp = self.client.api.build(path=build_path, dockerfile=self.image,
                                         tag="crossinstaller-{}".format(self.platform), quiet=False, rm=True)
            if isinstance(resp, str):
                return self.docker_run(self.client.images.get(resp))
            last_event = None
            image_id = None
            result_stream, internal_stream = itertools.tee(json_stream(resp))
            for chunk in internal_stream:
                if 'error' in chunk:
                    self.logger.error(chunk['error'])
                    raise BuildError(chunk['error'], result_stream)
                if 'stream' in chunk:
                    self.logger.debug(chunk['stream'])
                    match = re.search(
                        r'(^Successfully built |sha256:)([0-9a-f]+)$',
                        chunk['stream']
                    )
                    if match:
                        image_id = match.group(2)
                last_event = chunk
            if image_id:
                return self.docker_run(image_id)
            raise BuildError(last_event or 'Unknown', result_stream)
        except Exception as e:
            self.logger.error("An exception occurred while starting Docker image building process: {}".format(e))
            return

    def docker_run(self, image_id: str):
        if self.canceled:
            self.logger.info("Cancelled Docker run")
            self.cleanup()
            return

        try:
            # create and run container
            self.logger.info("Creating Docker container from image: {}".format(image_id))
            container_id = self.client.api.create_container(image_id, self.command, detach=False,
                                                            volumes=['/src'],
                                                            host_config=self.client.api.create_host_config(binds={
                                                                self.script_dir: {
                                                                    'bind': '/src',
                                                                    'mode': 'rw',
                                                                }
                                                            }))

            self.container = self.client.containers.get(container_id['Id'])
            self.logger.info("Starting Docker container")
            self.container.start()
            self.logger.info("Started building process")

            # print logs - not thread safe?
            # logs = container.attach(stdout=True, stderr=True, stream=True, logs=True)
            # for log in logs:
            #     self.logger.debug(str(log, encoding="utf-8"))
        except Exception as e:
            self.logger.error("An exception occurred while starting Docker container: {}".format(e))

        exit_status = self.container.wait()['StatusCode']
        self.logger.info("{} completed with exit code {}".format(self.platform, exit_status))
        self.cleanup()

    def cleanup(self):
        try:
            if not self.keep_build:
                shutil.rmtree(self.working_dir)
            self.logger.info("Cleanup successfull.")
        except Exception as e:
            self.logger.error("Error while cleaning after build: {}".format(e))
        self.finished = True

    def stop(self):
        self.canceled = True
        if self.container:
            self.container.stop()
