"""
 @file
 @brief Build server used to generate daily builds of libopenshot-audio, libopenshot, and openshot-qt
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2016 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

import os
import datetime
import platform
import shutil
from classes import info
from slacker import Slacker
import stat
import subprocess
import sys
import tinys3
import traceback


needs_build = False
freeze_command = None
output_lines = []
errors_detected = []
make_command = "make"
project_paths = []
slack_token = None
slack_object = None
s3_access_key = None
s3_secret_key = None
s3_connection = None


# Determine the paths and cmake args for each platform
if platform.system() == "Linux":
    freeze_command = "python3 /home/jonathan/apps/openshot-qt-git/freeze.py build"
    project_paths = [("/home/jonathan/apps/libopenshot-audio-git", "../", "https://github.com/OpenShot/libopenshot-audio.git"),
                     ("/home/jonathan/apps/libopenshot-git", "../", "https://github.com/OpenShot/libopenshot.git"),
                     ("/home/jonathan/apps/openshot-qt-git", "", "https://github.com/OpenShot/openshot-qt.git")]

elif platform.system() == "Darwin":
    freeze_command = "python3 /home/jonathan/apps/openshot-qt-git/freeze.py build-dmg"
    project_paths = [("/users/jonathan/apps/libopenshot-audio-git", "../", "https://github.com/OpenShot/libopenshot-audio.git"),
                     ("/users/jonathan/apps/libopenshot-git", "../", "https://github.com/OpenShot/libopenshot.git"),
                     ("/users/jonathan/apps/openshot-qt-git", "", "https://github.com/OpenShot/openshot-qt.git")]

elif platform.system() == "Windows":
    make_command = "mingw32-make"
    freeze_command = "python C:\\Users\\Jonathan\\apps\\openshot-qt-gitfreeze.py build"
    project_paths = [("C:\\Users\\Jonathan\\apps\\libopenshot-audio-git", "../", "https://github.com/OpenShot/libopenshot-audio.git"),
                     ("C:\\Users\\Jonathan\\apps\\libopenshot-git", "../", "https://github.com/OpenShot/libopenshot.git"),
                     ("C:\\Users\\Jonathan\\apps\\openshot-qt-git", "", "https://github.com/OpenShot/openshot-qt.git")]


def run_command(command):
    """Utility function to return output from command line"""
    p = subprocess.Popen(command, shell=True,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    return iter(p.stdout.readline, b"")

def output(line):
    """Append output to list and print it"""
    print(line)
    output_lines.append(line)

def error(line):
    """Append error output to list and print it"""
    print("Error: %s" % line)
    errors_detected.append(line)

def slack(message):
    """Append a message to slack #build-server channel"""
    print("Slack: %s" % message)
    if slack_object:
        slack_object.chat.post_message("#build-server", message)

def upload(file_path, s3_bucket):
    """Upload a file to S3"""
    if s3_connection:
        folder_path, file_name = os.path.split(file_path)
        with open(file_path, "rb") as f:
            s3_connection.upload(file_name, f, s3_bucket)


try:
    # Validate command-line arguments
    # argv[1] = Slack_token
    # argv[2] = S3 access key
    # argv[3] = S3 secret key
    if len(sys.argv) >= 2:
        slack_token = sys.argv[1]
        slack_object = Slacker(slack_token)
    if len(sys.argv) >= 4:
        s3_access_key = sys.argv[2]
        s3_secret_key = sys.argv[3]
        s3_connection = tinys3.Connection(s3_access_key, s3_secret_key, tls=True)


    # Loop through projects
    for project_path, cmake_args, git_origin in project_paths:
        # Change os directory
        os.chdir(project_path)

        # Check for new version in git
        needs_update = True
        for line in run_command("git fetch -v --dry-run"):
            output(line)
            if "[up to date]".encode("UTF-8") in line:
                needs_update = False
                break

        if needs_update:
            # Since something needs updating, we need to build the entire app again
            needs_build = True

            # Get latest from git
            for line in run_command("git pull %s" % git_origin):
                output(line)

            # Remove build folder & re-create it
            build_folder = os.path.join(project_path, "build")
            if os.path.exists(build_folder):
                shutil.rmtree(build_folder)
            os.makedirs(build_folder)

            # Change to build folder
            os.chdir(build_folder)

            # Skip to next project if no cmake args are found (only some projects need to be compiled)
            if not cmake_args:
                output("Skipping compilation for %s" % project_path)
                continue

            # Run CMAKE (configure all project files, and get ready to compile)
            for line in run_command("cmake %s" % cmake_args):
                output(line)
                if "CMake Error".encode("UTF-8") in line or "Configuring incomplete".encode("UTF-8") in line:
                    error("Cmake Error: %s" % line)

            # Run MAKE (compile binaries, python bindings, executables, etc...)
            for line in run_command(make_command):
                output(line)
                if ": error:".encode("UTF-8") in line or "No targets specified".encode("UTF-8") in line:
                    error("Make Error: %s" % line)

            # Run MAKE INSTALL (copy binaries to install directory)
            for line in run_command("%s install" % make_command):
                output(line)
                if "[install] Error".encode("UTF-8") in line or "CMake Error".encode("UTF-8") in line:
                    error("Make Install Error: %s" % line)


    # Now that everything is compiled, let's create the installers
    if not errors_detected: # and needs_build:
        # Change to openshot-qt dir
        project_path = project_paths[2][0]
        os.chdir(project_path)

        # Check for left over openshot-qt dupe folder
        if os.path.exists(os.path.join(project_path, "openshot_qt")):
            shutil.rmtree(os.path.join(project_path, "openshot_qt"))
        if os.path.exists(os.path.join(project_path, "build")):
            shutil.rmtree(os.path.join(project_path, "build"))

        # Successfully compiled - Time to create installers
        if platform.system() == "Linux":
            # Freeze it
            for line in run_command(freeze_command):
                output(line)
                if "logger:ERROR".encode("UTF-8") in line and not "importlib/__init__.pyc".encode("UTF-8") in line and not "zinfo".encode("UTF-8") in line:
                    error("Freeze Error: %s" % line)

            # Find exe folder
            exe_dirs = os.listdir(os.path.join(project_path, "build"))
            if len(exe_dirs) == 1:
                exe_dir = exe_dirs[0]

                # Create AppDir folder
                app_dir_path = os.path.join(project_path, "build", "OpenShot.AppDir")
                os.mkdir(app_dir_path)
                os.mkdir(os.path.join(app_dir_path, "usr"))
                os.mkdir(os.path.join(app_dir_path, "usr", "share"))
                os.mkdir(os.path.join(app_dir_path, "usr", "share", "pixmaps"))
                os.mkdir(os.path.join(app_dir_path, "usr", "share", "mime"))
                os.mkdir(os.path.join(app_dir_path, "usr", "share", "mime", "packages"))
                os.mkdir(os.path.join(app_dir_path, "usr", "lib"))
                os.mkdir(os.path.join(app_dir_path, "usr", "lib", "mime"))
                os.mkdir(os.path.join(app_dir_path, "usr", "lib", "mime", "packages"))

                # Create AppRun file
                app_run_path = os.path.join(app_dir_path, "AppRun")
                shutil.copyfile("/home/jonathan/apps/AppImageKit/AppRun", app_run_path)

                # Create .desktop file
                with open(os.path.join(app_dir_path, "openshot-qt.desktop"), "w") as f:
                    f.write('[Desktop Entry]\nName=OpenShot Video Editor\nGenericName=Video Editor\nX-GNOME-FullName=OpenShot Video Editor\nComment=Create and edit amazing videos and movies\nExec=openshot-qt.wrapper %F\nTerminal=false\nIcon=openshot-qt\nType=Application')

                # Copy some installation-related files
                shutil.copyfile(os.path.join(project_path, "xdg", "openshot-qt.svg"), os.path.join(app_dir_path, "openshot-qt.svg"))
                shutil.copyfile(os.path.join(project_path, "xdg", "openshot-qt.svg"), os.path.join(app_dir_path, "usr", "share", "pixmaps", "openshot-qt.svg"))
                shutil.copyfile(os.path.join(project_path, "xdg", "openshot-qt.xml"), os.path.join(app_dir_path, "usr", "share", "mime", "packages", "openshot-qt.xml"))
                shutil.copyfile(os.path.join(project_path, "xdg", "openshot-qt"), os.path.join(app_dir_path, "usr", "lib", "mime", "packages", "openshot-qt"))

                # Copy the entire frozen app
                shutil.copytree(os.path.join(project_path, "build", exe_dir), os.path.join(app_dir_path, "usr", "bin"))

                # Copy desktop integration wrapper (prompts users to install shortcut)
                launcher_path = os.path.join(app_dir_path, "usr", "bin", "openshot-qt")
                os.rename(os.path.join(app_dir_path, "usr", "bin", "launch-linux.sh"), launcher_path)
                desktop_wrapper = os.path.join(app_dir_path, "usr", "bin", "openshot-qt.wrapper")
                shutil.copyfile("/home/jonathan/apps/AppImageKit/desktopintegration", desktop_wrapper)

                # Change permission of AppRun (and desktop.wrapper) file (add execute permission)
                st = os.stat(app_run_path)
                os.chmod(app_run_path, st.st_mode | stat.S_IEXEC)
                os.chmod(desktop_wrapper, st.st_mode | stat.S_IEXEC)
                os.chmod(launcher_path, st.st_mode | stat.S_IEXEC)

                # Create AppImage (OpenShot-%s-x86_64.AppImage)
                app_image_success = False
                app_name = "OpenShot-%s-%s-x86_64.AppImage" % (info.VERSION, datetime.datetime.now().strftime("%Y-%m-%d"))
                app_image_path = os.path.join(project_path, "build", app_name)
                for line in run_command('/home/jonathan/apps/AppImageKit/AppImageAssistant "%s" "%s"' % (app_dir_path, app_image_path)):
                    output(line)
                    if "error".encode("UTF-8") in line:
                        error("AppImageKit Error: %s" % line)
                    if "completed sucessfully".encode("UTF-8") in line:
                        app_image_success = True

                # Was the AppImage creation successful
                if app_image_success:
                    # Check if AppImage exists
                    if os.path.exists(app_image_path):
                        # Upload file to S3
                        output("S3: Uploading %s to Amazon S3" % app_image_path)
                        upload(app_image_path, "releases.openshot.org/linux")

                        # Notify Slack
                        slack("%s: Successful build: http://releases.openshot.org/linux/%s" % (platform.system(), app_name))

                    else:
                        # AppImage doesn't exist
                        error("AppImageKit Error: %s does not exist" % app_image_path)
                else:
                    # AppImage failed
                    error("AppImageKit Error: AppImageAssistant did not output 'completed successfully'")


        if platform.system() == "Darwin":
            pass

        if platform.system() == "Windows":
            pass

except Exception as ex:
    tb = traceback.format_exc()
    error("Unhandled exception: %s - %s" % (str(ex), str(tb)))


# Report any errors detected
if errors_detected:
    slack("%s: Build errors were detected: %s" % (platform.system(), errors_detected))
else:
    output("Successful build server run!")


















