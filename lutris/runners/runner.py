"""Base module for runners"""
import os
import signal

from gettext import gettext as _

from gi.repository import Gtk

from lutris import runtime, settings
from lutris.command import MonitoredCommand
from lutris.config import LutrisConfig
from lutris.database.games import get_game_by_field
from lutris.exceptions import UnavailableLibrariesError
from lutris.gui import dialogs
from lutris.runners import RunnerInstallationError
from lutris.util import system
from lutris.util.extract import ExtractFailure, extract_archive
from lutris.util.http import HTTPError, Request
from lutris.util.linux import LINUX_SYSTEM
from lutris.util.log import logger


class Runner:  # pylint: disable=too-many-public-methods

    """Generic runner (base class for other runners)."""

    multiple_versions = False
    platforms = []
    runnable_alone = False
    game_options = []
    runner_options = []
    system_options_override = []
    context_menu_entries = []
    require_libs = []
    runner_executable = None
    entry_point_option = "main_file"
    download_url = None
    arch = None  # If the runner is only available for an architecture that isn't x86_64

    def __init__(self, config=None):
        """Initialize runner."""
        if config:
            self.has_explicit_config = True
            self._config = config
            self.game_data = get_game_by_field(config.game_config_id, "configpath")
        else:
            self.has_explicit_config = False
            self._config = None
            self.game_data = {}

    def __lt__(self, other):
        return self.name < other.name

    @property
    def description(self):
        """Return the class' docstring as the description."""
        return self.__doc__

    @description.setter
    def description(self, value):
        """Leave the ability to override the docstring."""
        self.__doc__ = value  # What the shit

    @property
    def name(self):
        return self.__class__.__name__

    @property
    def directory(self):
        return os.path.join(settings.RUNNER_DIR, self.name)

    @property
    def config(self):
        if not self._config:
            self._config = LutrisConfig(runner_slug=self.name)
        return self._config

    @config.setter
    def config(self, new_config):
        self._config = new_config
        self.has_explicit_config = new_config is not None

    @property
    def game_config(self):
        """Return the cascaded game config as a dict."""
        if not self.has_explicit_config:
            logger.warning("Accessing game config while runner wasn't given one.")

        return self.config.game_config

    @property
    def runner_config(self):
        """Return the cascaded runner config as a dict."""
        return self.config.runner_config

    @property
    def system_config(self):
        """Return the cascaded system config as a dict."""
        return self.config.system_config

    @property
    def default_path(self):
        """Return the default path where games are installed."""
        return self.system_config.get("game_path")

    @property
    def game_path(self):
        """Return the directory where the game is installed."""
        game_path = self.game_data.get("directory")
        if game_path:
            return game_path

        if self.has_explicit_config:
            # Default to the directory where the entry point is located.
            entry_point = self.game_config.get(self.entry_point_option)
            if entry_point:
                return os.path.dirname(os.path.expanduser(entry_point))
        return ""

    def resolve_game_path(self):
        """Returns the path where the game is found; if game_path does not
        provide a path, this may try to resolve the path by runner-specific means,
        which can find things like /usr/games when applicable."""
        return self.game_path

    @property
    def library_folders(self):
        """Return a list of paths where a game might be installed"""
        return []

    @property
    def working_dir(self):
        """Return the working directory to use when running the game."""
        return self.game_path or os.path.expanduser("~/")

    @property
    def shader_cache_dir(self):
        """Return the cache directory for this runner to use. We create
        this if it does not exist."""
        path = os.path.join(settings.SHADER_CACHE_DIR, self.name)
        if not os.path.isdir(path):
            os.mkdir(path)
        return path

    @property
    def nvidia_shader_cache_path(self):
        """The path to place in __GL_SHADER_DISK_CACHE_PATH; NVidia
        will place its cache cache in a subdirectory here."""
        return self.shader_cache_dir

    @property
    def discord_client_id(self):
        if self.game_data.get("discord_client_id"):
            return self.game_data.get("discord_client_id")

    def get_platform(self):
        return self.platforms[0]

    def get_runner_options(self):
        runner_options = self.runner_options[:]
        if self.runner_executable:
            runner_options.append(
                {
                    "option": "runner_executable",
                    "type": "file",
                    "label": _("Custom executable for the runner"),
                    "advanced": True,
                }
            )
        return runner_options

    def get_executable(self):
        if "runner_executable" in self.runner_config:
            runner_executable = self.runner_config["runner_executable"]
            if os.path.isfile(runner_executable):
                return runner_executable
        if not self.runner_executable:
            raise ValueError("runner_executable not set for {}".format(self.name))
        return os.path.join(settings.RUNNER_DIR, self.runner_executable)

    def get_env(self, os_env=False, disable_runtime=False):
        """Return environment variables used for a game."""
        env = {}
        if os_env:
            env.update(os.environ.copy())

        # By default we'll set NVidia's shader disk cache to be
        # per-game, so it overflows less readily.
        env["__GL_SHADER_DISK_CACHE"] = "1"
        env["__GL_SHADER_DISK_CACHE_PATH"] = self.nvidia_shader_cache_path

        # Override SDL2 controller configuration
        sdl_gamecontrollerconfig = self.system_config.get("sdl_gamecontrollerconfig")
        if sdl_gamecontrollerconfig:
            path = os.path.expanduser(sdl_gamecontrollerconfig)
            if system.path_exists(path):
                with open(path, "r", encoding='utf-8') as controllerdb_file:
                    sdl_gamecontrollerconfig = controllerdb_file.read()
            env["SDL_GAMECONTROLLERCONFIG"] = sdl_gamecontrollerconfig

        # Set monitor to use for SDL 1 games
        sdl_video_fullscreen = self.system_config.get("sdl_video_fullscreen")
        if sdl_video_fullscreen and sdl_video_fullscreen != "off":
            env["SDL_VIDEO_FULLSCREEN_DISPLAY"] = sdl_video_fullscreen

        # DRI Prime
        if self.system_config.get("dri_prime"):
            env["DRI_PRIME"] = "1"

        # Prime vars
        prime = self.system_config.get("prime")
        if prime:
            env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
            env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
            env["__VK_LAYER_NV_optimus"] = "NVIDIA_only"

        # Set PulseAudio latency to 60ms
        if self.system_config.get("pulse_latency"):
            env["PULSE_LATENCY_MSEC"] = "60"

        # Vulkan ICD files
        vk_icd = self.system_config.get("vk_icd")
        if vk_icd:
            env["VK_ICD_FILENAMES"] = vk_icd

        runtime_ld_library_path = None

        if not disable_runtime and self.use_runtime():
            runtime_env = self.get_runtime_env()
            runtime_ld_library_path = runtime_env.get("LD_LIBRARY_PATH")

        if runtime_ld_library_path:
            ld_library_path = env.get("LD_LIBRARY_PATH")
            env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [
                runtime_ld_library_path, ld_library_path]))

        # Apply user overrides at the end
        env.update(self.system_config.get("env") or {})

        return env

    def get_runtime_env(self):
        """Return runtime environment variables.

        This method may be overridden in runner classes.
        (Notably for Lutris wine builds)

        Returns:
            dict

        """
        return runtime.get_env(prefer_system_libs=self.system_config.get("prefer_system_libs", True))

    def prelaunch(self):
        """Run actions before running the game, override this method in runners; raise an
        exception if prelaunch fails, and it will be reported to the user, and
        then the game won't start."""
        available_libs = set()
        for lib in set(self.require_libs):
            if lib in LINUX_SYSTEM.shared_libraries:
                if self.arch:
                    if self.arch in [_lib.arch for _lib in LINUX_SYSTEM.shared_libraries[lib]]:
                        available_libs.add(lib)
                else:
                    available_libs.add(lib)
        unavailable_libs = set(self.require_libs) - available_libs
        if unavailable_libs:
            raise UnavailableLibrariesError(unavailable_libs, self.arch)

    def get_run_data(self):
        """Return dict with command (exe & args list) and env vars (dict).

        Reimplement in derived runner if need be."""
        return {"command": [self.get_executable()], "env": self.get_env()}

    def run(self, *args):
        """Run the runner alone."""
        if not self.runnable_alone:
            return
        if not self.is_installed():
            if not self.install_dialog():
                logger.info("Runner install cancelled")
                return

        command_data = self.get_run_data()
        command = command_data.get("command")
        env = (command_data.get("env") or {}).copy()

        if hasattr(self, "prelaunch"):
            self.prelaunch()

        command_runner = MonitoredCommand(command, runner=self, env=env)
        command_runner.start()

    def use_runtime(self):
        if runtime.RUNTIME_DISABLED:
            logger.info("Runtime disabled by environment")
            return False
        if self.system_config.get("disable_runtime"):
            logger.info("Runtime disabled by system configuration")
            return False
        return True

    def install_dialog(self):
        """Ask the user if they want to install the runner.

        Return success of runner installation.
        """
        dialog = dialogs.QuestionDialog(
            {
                "question": _("The required runner is not installed.\n"
                              "Do you wish to install it now?"),
                "title": _("Required runner unavailable"),
            }
        )
        if Gtk.ResponseType.YES == dialog.result:

            from lutris.gui.dialogs import ErrorDialog
            from lutris.gui.dialogs.download import simple_downloader
            try:
                if hasattr(self, "get_version"):
                    version = self.get_version(use_default=False)  # pylint: disable=no-member
                    self.install(downloader=simple_downloader, version=version)
                else:
                    self.install(downloader=simple_downloader)
            except RunnerInstallationError as ex:
                ErrorDialog(ex.message)

            return self.is_installed()
        return False

    def is_installed(self):
        """Return whether the runner is installed"""
        return system.path_exists(self.get_executable())

    def get_runner_version(self, version=None):
        """Get the appropriate version for a runner

        Params:
            version (str): Optional version to lookup, will return this one if found

        Returns:
            dict: Dict containing version, architecture and url for the runner, None
            if the data can't be retrieved.
        """
        logger.info(
            "Getting runner information for %s%s",
            self.name,
            " (version: %s)" % version if version else "",
        )

        try:
            request = Request("{}/api/runners/{}".format(settings.SITE_URL, self.name))
            runner_info = request.get().json

            if not runner_info:
                logger.error("Failed to get runner information")
        except HTTPError as ex:
            logger.error("Unable to get runner information: %s", ex)
            runner_info = None

        if not runner_info:
            return

        versions = runner_info.get("versions") or []
        arch = LINUX_SYSTEM.arch
        if version:
            if version.endswith("-i386") or version.endswith("-x86_64"):
                version, arch = version.rsplit("-", 1)
            versions = [v for v in versions if v["version"] == version]
        versions_for_arch = [v for v in versions if v["architecture"] == arch]
        if len(versions_for_arch) == 1:
            return versions_for_arch[0]

        if len(versions_for_arch) > 1:
            default_version = [v for v in versions_for_arch if v["default"] is True]
            if default_version:
                return default_version[0]
        elif len(versions) == 1 and LINUX_SYSTEM.is_64_bit:
            return versions[0]
        elif len(versions) > 1 and LINUX_SYSTEM.is_64_bit:
            default_version = [v for v in versions if v["default"] is True]
            if default_version:
                return default_version[0]
        # If we didn't find a proper version yet, return the first available.
        if len(versions_for_arch) >= 1:
            return versions_for_arch[0]

    def install(self, version=None, downloader=None, callback=None):
        """Install runner using package management systems."""
        logger.debug(
            "Installing %s (version=%s, downloader=%s, callback=%s)",
            self.name,
            version,
            downloader,
            callback,
        )
        opts = {"downloader": downloader, "callback": callback}
        if self.download_url:
            opts["dest"] = self.directory
            return self.download_and_extract(self.download_url, **opts)
        runner = self.get_runner_version(version)
        if not runner:
            raise RunnerInstallationError(_("Failed to retrieve {} ({}) information").format(self.name, version))
        if not downloader:
            raise RuntimeError("Missing mandatory downloader for runner %s" % self)

        if "wine" in self.name:
            opts["merge_single"] = True
            opts["dest"] = os.path.join(
                self.directory, "{}-{}".format(runner["version"], runner["architecture"])
            )

        if self.name == "libretro" and version:
            opts["merge_single"] = False
            opts["dest"] = os.path.join(settings.RUNNER_DIR, "retroarch/cores")
        self.download_and_extract(runner["url"], **opts)

    def download_and_extract(self, url, dest=None, **opts):
        downloader = opts["downloader"]
        merge_single = opts.get("merge_single", False)
        callback = opts.get("callback")
        tarball_filename = os.path.basename(url)
        runner_archive = os.path.join(settings.CACHE_DIR, tarball_filename)
        if not dest:
            dest = settings.RUNNER_DIR
        downloader(
            url, runner_archive, self.extract, {
                "archive": runner_archive,
                "dest": dest,
                "merge_single": merge_single,
                "callback": callback,
            }
        )

    def extract(self, archive=None, dest=None, merge_single=None, callback=None):
        if not system.path_exists(archive):
            raise RunnerInstallationError(_("Failed to extract {}").format(archive))
        try:
            extract_archive(archive, dest, merge_single=merge_single)
        except ExtractFailure as ex:
            logger.error("Failed to extract the archive %s file may be corrupt", archive)
            raise RunnerInstallationError(_("Failed to extract {}: {}").format(archive, ex)) from ex
        os.remove(archive)

        if self.name == "wine":
            logger.debug("Clearing wine version cache")
            from lutris.util.wine.wine import get_wine_versions
            get_wine_versions.cache_clear()

        if self.runner_executable:
            runner_executable = os.path.join(settings.RUNNER_DIR, self.runner_executable)
            if os.path.isfile(runner_executable):
                system.make_executable(runner_executable)

        if callback:
            callback()

    def remove_game_data(self, app_id=None, game_path=None):
        system.remove_folder(game_path)

    def can_uninstall(self):
        return os.path.isdir(self.directory)

    def uninstall(self):
        runner_path = self.directory
        if os.path.isdir(runner_path):
            system.remove_folder(runner_path)

    def find_option(self, options_group, option_name):
        """Retrieve an option dict if it exists in the group"""
        if options_group not in ['game_options', 'runner_options']:
            return None
        output = None
        for item in getattr(self, options_group):
            if item["option"] == option_name:
                output = item
                break
        return output

    def force_stop_game(self, game):
        """Stop the running game. If this leaves any game processes running,
        the caller will SIGKILL them (after a delay)."""
        game.kill_processes(signal.SIGTERM)
