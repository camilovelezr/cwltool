"""Enables Docker software containers via the {dx-,u,}docker runtimes."""

import csv
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from distutils import spawn
from io import StringIO, open  # pylint: disable=redefined-builtin
from typing import Callable, Dict, List, MutableMapping, Optional, Set, Tuple

import requests

from .builder import Builder
from .context import RuntimeContext
from .docker_id import docker_vm_id
from .errors import WorkflowException
from .expression import JSON
from .job import ContainerCommandLineJob
from .loghandler import _logger
from .pathmapper import MapperEnt, PathMapper, ensure_non_writable, ensure_writable
from .utils import DEFAULT_TMP_PREFIX, docker_windows_path_adjust, onWindows

_IMAGES = set()  # type: Set[str]
_IMAGES_LOCK = threading.Lock()
__docker_machine_mounts = None  # type: Optional[List[str]]
__docker_machine_mounts_lock = threading.Lock()


def _get_docker_machine_mounts():  # type: () -> List[str]
    global __docker_machine_mounts
    if __docker_machine_mounts is None:
        with __docker_machine_mounts_lock:
            if "DOCKER_MACHINE_NAME" not in os.environ:
                __docker_machine_mounts = []
            else:
                __docker_machine_mounts = [
                    "/" + line.split(None, 1)[0]
                    for line in subprocess.check_output(
                        [
                            "docker-machine",
                            "ssh",
                            os.environ["DOCKER_MACHINE_NAME"],
                            "mount",
                            "-t",
                            "vboxsf",
                        ],
                        universal_newlines=True,
                    ).splitlines()
                ]
    return __docker_machine_mounts


def _check_docker_machine_path(path):  # type: (Optional[str]) -> None
    if path is None:
        return
    if onWindows():
        path = path.lower()
    mounts = _get_docker_machine_mounts()

    found = False
    for mount in mounts:
        if onWindows():
            mount = mount.lower()
        if path.startswith(mount):
            found = True
            break

    if not found and mounts:
        name = os.environ.get("DOCKER_MACHINE_NAME", "???")
        raise WorkflowException(
            "Input path {path} is not in the list of host paths mounted "
            "into the Docker virtual machine named {name}. Already mounted "
            "paths: {mounts}.\n"
            "See https://docs.docker.com/toolbox/toolbox_install_windows/"
            "#optional-add-shared-directories for instructions on how to "
            "add this path to your VM.".format(path=path, name=name, mounts=mounts)
        )


class DockerCommandLineJob(ContainerCommandLineJob):
    """Runs a CommandLineJob in a sofware container using the Docker engine."""

    def __init__(
        self,
        builder: Builder,
        joborder: JSON,
        make_path_mapper: Callable[..., PathMapper],
        requirements: List[Dict[str, str]],
        hints: List[Dict[str, str]],
        name: str,
    ) -> None:
        super(DockerCommandLineJob, self).__init__(
            builder, joborder, make_path_mapper, requirements, hints, name
        )

    @staticmethod
    def get_image(
        docker_requirement,  # type: Dict[str, str]
        pull_image,  # type: bool
        force_pull=False,  # type: bool
        tmp_outdir_prefix=DEFAULT_TMP_PREFIX,  # type: str
    ):  # type: (...) -> bool
        """
        Retrieve the relevant Docker container image.

        Returns True upon success
        """
        found = False

        if (
            "dockerImageId" not in docker_requirement
            and "dockerPull" in docker_requirement
        ):
            docker_requirement["dockerImageId"] = docker_requirement["dockerPull"]

        with _IMAGES_LOCK:
            if docker_requirement["dockerImageId"] in _IMAGES:
                return True

        for line in (
            subprocess.check_output(["docker", "images", "--no-trunc", "--all"])
            .decode("utf-8")
            .splitlines()
        ):
            try:
                match = re.match(r"^([^ ]+)\s+([^ ]+)\s+([^ ]+)", line)
                split = docker_requirement["dockerImageId"].split(":")
                if len(split) == 1:
                    split.append("latest")
                elif len(split) == 2:
                    #  if split[1] doesn't  match valid tag names, it is a part of repository
                    if not re.match(r"[\w][\w.-]{0,127}", split[1]):
                        split[0] = split[0] + ":" + split[1]
                        split[1] = "latest"
                elif len(split) == 3:
                    if re.match(r"[\w][\w.-]{0,127}", split[2]):
                        split[0] = split[0] + ":" + split[1]
                        split[1] = split[2]
                        del split[2]

                # check for repository:tag match or image id match
                if match and (
                    (split[0] == match.group(1) and split[1] == match.group(2))
                    or docker_requirement["dockerImageId"] == match.group(3)
                ):
                    found = True
                    break
            except ValueError:
                pass

        if (force_pull or not found) and pull_image:
            cmd = []  # type: List[str]
            if "dockerPull" in docker_requirement:
                cmd = ["docker", "pull", str(docker_requirement["dockerPull"])]
                _logger.info(str(cmd))
                subprocess.check_call(cmd, stdout=sys.stderr)
                found = True
            elif "dockerFile" in docker_requirement:
                dockerfile_dir = str(tempfile.mkdtemp(prefix=tmp_outdir_prefix))
                with open(os.path.join(dockerfile_dir, "Dockerfile"), "wb") as dfile:
                    dfile.write(docker_requirement["dockerFile"].encode("utf-8"))
                cmd = [
                    "docker",
                    "build",
                    "--tag=%s" % str(docker_requirement["dockerImageId"]),
                    dockerfile_dir,
                ]
                _logger.info(str(cmd))
                subprocess.check_call(cmd, stdout=sys.stderr)
                found = True
            elif "dockerLoad" in docker_requirement:
                cmd = ["docker", "load"]
                _logger.info(str(cmd))
                if os.path.exists(docker_requirement["dockerLoad"]):
                    _logger.info(
                        "Loading docker image from %s",
                        docker_requirement["dockerLoad"],
                    )
                    with open(docker_requirement["dockerLoad"], "rb") as dload:
                        loadproc = subprocess.Popen(cmd, stdin=dload, stdout=sys.stderr)
                else:
                    loadproc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=sys.stderr
                    )
                    assert loadproc.stdin is not None  # nosec
                    _logger.info(
                        "Sending GET request to %s", docker_requirement["dockerLoad"]
                    )
                    req = requests.get(docker_requirement["dockerLoad"], stream=True)
                    size = 0
                    for chunk in req.iter_content(1024 * 1024):
                        size += len(chunk)
                        _logger.info("\r%i bytes", size)
                        loadproc.stdin.write(chunk)
                    loadproc.stdin.close()
                rcode = loadproc.wait()
                if rcode != 0:
                    raise WorkflowException(
                        "Docker load returned non-zero exit status %i" % (rcode)
                    )
                found = True
            elif "dockerImport" in docker_requirement:
                cmd = [
                    "docker",
                    "import",
                    str(docker_requirement["dockerImport"]),
                    str(docker_requirement["dockerImageId"]),
                ]
                _logger.info(str(cmd))
                subprocess.check_call(cmd, stdout=sys.stderr)
                found = True

        if found:
            with _IMAGES_LOCK:
                _IMAGES.add(docker_requirement["dockerImageId"])

        return found

    def get_from_requirements(
        self,
        r,  # type: Dict[str, str]
        pull_image,  # type: bool
        force_pull=False,  # type: bool
        tmp_outdir_prefix=DEFAULT_TMP_PREFIX,  # type: str
    ):  # type: (...) -> Optional[str]
        if not spawn.find_executable("docker"):
            raise WorkflowException("docker executable is not available")

        if self.get_image(r, pull_image, force_pull, tmp_outdir_prefix):
            return r["dockerImageId"]
        raise WorkflowException("Docker image %s not found" % r["dockerImageId"])

    @staticmethod
    def append_volume(runtime, source, target, writable=False):
        # type: (List[str], str, str, bool) -> None
        """Add binding arguments to the runtime list."""
        options = [
            "type=bind",
            "source=" + source,
            "target=" + target,
        ]
        if not writable:
            options.append("readonly")
        output = StringIO()
        csv.writer(output).writerow(options)
        mount_arg = output.getvalue().strip()
        runtime.append("--mount={}".format(mount_arg))
        # Unlike "--volume", "--mount" will fail if the volume doesn't already exist.
        if not os.path.exists(source):
            os.makedirs(source)

    def add_file_or_directory_volume(
        self, runtime: List[str], volume: MapperEnt, host_outdir_tgt: Optional[str]
    ) -> None:
        """Append volume a file/dir mapping to the runtime option list."""
        if not volume.resolved.startswith("_:"):
            _check_docker_machine_path(docker_windows_path_adjust(volume.resolved))
            self.append_volume(runtime, volume.resolved, volume.target)

    def add_writable_file_volume(
        self,
        runtime,  # type: List[str]
        volume,  # type: MapperEnt
        host_outdir_tgt,  # type: Optional[str]
        tmpdir_prefix,  # type: str
    ):  # type: (...) -> None
        """Append a writable file mapping to the runtime option list."""
        if self.inplace_update:
            self.append_volume(runtime, volume.resolved, volume.target, writable=True)
        else:
            if host_outdir_tgt:
                # shortcut, just copy to the output directory
                # which is already going to be mounted
                if not os.path.exists(os.path.dirname(host_outdir_tgt)):
                    os.makedirs(os.path.dirname(host_outdir_tgt))
                shutil.copy(volume.resolved, host_outdir_tgt)
            else:
                tmp_dir, tmp_prefix = os.path.split(tmpdir_prefix)
                tmpdir = tempfile.mkdtemp(prefix=tmp_prefix, dir=tmp_dir)
                file_copy = os.path.join(tmpdir, os.path.basename(volume.resolved))
                shutil.copy(volume.resolved, file_copy)
                self.append_volume(runtime, file_copy, volume.target, writable=True)
            ensure_writable(host_outdir_tgt or file_copy)

    def add_writable_directory_volume(
        self,
        runtime,  # type: List[str]
        volume,  # type: MapperEnt
        host_outdir_tgt,  # type: Optional[str]
        tmpdir_prefix,  # type: str
    ):  # type: (...) -> None
        """Append a writable directory mapping to the runtime option list."""
        if volume.resolved.startswith("_:"):
            # Synthetic directory that needs creating first
            if not host_outdir_tgt:
                tmp_dir, tmp_prefix = os.path.split(tmpdir_prefix)
                new_dir = os.path.join(
                    tempfile.mkdtemp(prefix=tmp_prefix, dir=tmp_dir),
                    os.path.basename(volume.target),
                )
                self.append_volume(runtime, new_dir, volume.target, writable=True)
            elif not os.path.exists(host_outdir_tgt):
                os.makedirs(host_outdir_tgt)
        else:
            if self.inplace_update:
                self.append_volume(
                    runtime, volume.resolved, volume.target, writable=True
                )
            else:
                if not host_outdir_tgt:
                    tmp_dir, tmp_prefix = os.path.split(tmpdir_prefix)
                    tmpdir = tempfile.mkdtemp(prefix=tmp_prefix, dir=tmp_dir)
                    new_dir = os.path.join(tmpdir, os.path.basename(volume.resolved))
                    shutil.copytree(volume.resolved, new_dir)
                    self.append_volume(runtime, new_dir, volume.target, writable=True)
                else:
                    shutil.copytree(volume.resolved, host_outdir_tgt)
                ensure_writable(host_outdir_tgt or new_dir)

    def create_runtime(
        self, env: MutableMapping[str, str], runtimeContext: RuntimeContext
    ) -> Tuple[List[str], Optional[str]]:
        any_path_okay = self.builder.get_requirement("DockerRequirement")[1] or False
        user_space_docker_cmd = runtimeContext.user_space_docker_cmd
        if user_space_docker_cmd:
            if "udocker" in user_space_docker_cmd and not runtimeContext.debug:
                runtime = [user_space_docker_cmd, "--quiet", "run"]
                # udocker 1.1.1 will output diagnostic messages to stdout
                # without this
            else:
                runtime = [user_space_docker_cmd, "run"]
        else:
            runtime = ["docker", "run", "-i"]
        self.append_volume(
            runtime, os.path.realpath(self.outdir), self.builder.outdir, writable=True
        )
        tmpdir = "/tmp"  # nosec
        self.append_volume(
            runtime, os.path.realpath(self.tmpdir), tmpdir, writable=True
        )
        self.add_volumes(
            self.pathmapper,
            runtime,
            any_path_okay=True,
            secret_store=runtimeContext.secret_store,
            tmpdir_prefix=runtimeContext.tmpdir_prefix,
        )
        if self.generatemapper is not None:
            self.add_volumes(
                self.generatemapper,
                runtime,
                any_path_okay=any_path_okay,
                secret_store=runtimeContext.secret_store,
                tmpdir_prefix=runtimeContext.tmpdir_prefix,
            )

        if user_space_docker_cmd:
            runtime = [x.replace(":ro", "") for x in runtime]
            runtime = [x.replace(":rw", "") for x in runtime]

        runtime.append(
            "--workdir=%s" % (docker_windows_path_adjust(self.builder.outdir))
        )
        if not user_space_docker_cmd:

            if not runtimeContext.no_read_only:
                runtime.append("--read-only=true")

            if self.networkaccess:
                if runtimeContext.custom_net:
                    runtime.append("--net={0}".format(runtimeContext.custom_net))
            else:
                runtime.append("--net=none")

            if self.stdout is not None:
                runtime.append("--log-driver=none")

            euid, egid = docker_vm_id()
            if not onWindows():
                # MS Windows does not have getuid() or geteuid() functions
                euid, egid = euid or os.geteuid(), egid or os.getgid()

            if runtimeContext.no_match_user is False and (
                euid is not None and egid is not None
            ):
                runtime.append("--user=%d:%d" % (euid, egid))

        if runtimeContext.rm_container:
            runtime.append("--rm")

        runtime.append("--env=TMPDIR=/tmp")

        # spec currently says "HOME must be set to the designated output
        # directory." but spec might change to designated temp directory.
        # runtime.append("--env=HOME=/tmp")
        runtime.append("--env=HOME=%s" % self.builder.outdir)

        cidfile_path = None  # type: Optional[str]
        # add parameters to docker to write a container ID file
        if runtimeContext.user_space_docker_cmd is None:
            if runtimeContext.cidfile_dir:
                cidfile_dir = runtimeContext.cidfile_dir
                if not os.path.exists(str(cidfile_dir)):
                    _logger.error(
                        "--cidfile-dir %s error:\n%s",
                        cidfile_dir,
                        "directory doesn't exist, please create it first",
                    )
                    exit(2)
                if not os.path.isdir(cidfile_dir):
                    _logger.error(
                        "--cidfile-dir %s error:\n%s",
                        cidfile_dir,
                        cidfile_dir + " is not a directory, " "please check it first",
                    )
                    exit(2)
            else:
                tmp_dir, tmp_prefix = os.path.split(runtimeContext.tmpdir_prefix)
                cidfile_dir = tempfile.mkdtemp(prefix=tmp_prefix, dir=tmp_dir)

            cidfile_name = datetime.datetime.now().strftime("%Y%m%d%H%M%S-%f") + ".cid"
            if runtimeContext.cidfile_prefix is not None:
                cidfile_name = str(runtimeContext.cidfile_prefix + "-" + cidfile_name)
            cidfile_path = os.path.join(cidfile_dir, cidfile_name)
            runtime.append("--cidfile=%s" % cidfile_path)
        for key, value in self.environment.items():
            runtime.append("--env=%s=%s" % (key, value))

        if runtimeContext.strict_memory_limit and not user_space_docker_cmd:
            runtime.append("--memory=%dm" % self.builder.resources["ram"])
        elif not user_space_docker_cmd:
            res_req, _ = self.builder.get_requirement("ResourceRequirement")
            if res_req and ("ramMin" in res_req or "ramMax" in res_req):
                _logger.warning(
                    "[job %s] Skipping Docker software container '--memory' limit "
                    "despite presence of ResourceRequirement with ramMin "
                    "and/or ramMax setting. Consider running with "
                    "--strict-memory-limit for increased portability "
                    "assurance.",
                    self.name,
                )

        return runtime, cidfile_path
