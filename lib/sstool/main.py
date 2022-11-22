import argparse
import os
import os.path
import shutil
import subprocess
import sys

import jinja2
import yaml

tool_prefix = ''

def make_argparser():
    parser = argparse.ArgumentParser(description='Generate a build configuration for a spack stack from a recipe.')
    parser.add_argument('-b', '--build',
            required=True,
            type=str)
    parser.add_argument('-r', '--recipe',
            required=True,
            type=str)
    parser.add_argument('-d', '--debug', action='store_true')
    return parser

def validate_recipe_config(config):
    # TODO: create config error type
    if "cpu" not in config.keys():
        raise FileNotFoundError('config.yaml: missing cpu')
    if config["cpu"] not in ["zen3", "zen2"]:
        raise FileNotFoundError('config.yaml: invalid target cpu architecture "{0}"'.format(config["cpu"]))
    if "gpu" not in config.keys():
        config["gpu"] = "NONE"
    if config["gpu"] not in ["A100", "Mi200", "NONE"]:
        raise FileNotFoundError('config.yaml: invalid target gpu architecture "{0}"'.format(config["gpu"]))
    return config

class Recipe:
    path = ''
    compilers = {}
    config = {}

    def __init__(self, args):
        path = args.recipe
        if not os.path.isabs(path):
            path = os.path.join(os.path.abspath(os.path.curdir), path)
        if not os.path.isdir(path):
            raise FileNotFoundError('The recipe path \'{path}\' does not exist'.format(path=path))
        self.path=path

        compiler_path = os.path.join(path, 'compilers.yaml')
        if not os.path.isfile(compiler_path):
            raise FileNotFoundError('The recipe path \'{path}\' does not contain compilers.yaml'.format(path=compiler_path))
        with open(compiler_path) as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            self.generate_compiler_specs(raw['compilers'])

        packages_path = os.path.join(path, 'packages.yaml')
        if not os.path.isfile(packages_path):
            raise FileNotFoundError('The recipe path \'{path}\' does not contain packages.yaml'.format(path=packages_path))
        with open(packages_path) as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            self.packages = raw['packages']

        config_path = os.path.join(path, 'config.yaml')
        if not os.path.isfile(config_path):
            raise FileNotFoundError('The recipe path \'{path}\' does not contain compilers.yaml'.format(path=config_path))
        with open(config_path) as fid:
            self.config = validate_recipe_config(yaml.load(fid, Loader=yaml.Loader))

        modules_path = os.path.join(path, 'modules.yaml')
        if not os.path.isfile(modules_path):
            modules_path = os.path.join(args.build, 'spack/etc/spack/defaults/modules.yaml')
        self.modules = modules_path

    def generate_modules(self):
        with open(self.modules) as fid:
            raw = yaml.load(fid, Loader=yaml.Loader)
            raw['modules']['default']['roots']['tcl'] = os.path.join(self.config['store'], 'modules')
            return yaml.dump(raw)

    # creates the self.compilers field that describes the full specifications
    # for all of teh compilers from the raw compilers.yaml input
    def generate_compiler_specs(self, raw):
        # TODO: error checking
        #   bootstrap and gcc have been specified
        #   gcc specs are of the form gcc@version
        #   llvm specs are of the form {llvm@version, nvhpc@version}
        # TODO: extend specs of compilers
        #   nvhpc@v ... nvhpc@v~blas~lapack~mpi
        #   llvm@v ... nvhpc@v targets=x86_64,nvptx +gold ...
        #   bootstrap gcc@v... gcc@v languages=c,c++
        # TODO: additional specs
        #   add "squashfs default_compression=zstd" to bootstrap specs
        compilers = {}

        bootstrap = {}
        bootstrap["packages"]= {
            "external": ["perl", "m4", "autoconf", "automake", "libtool", "gawk", "python"],
            "variants": {
                "gcc": "[build_type=Release ~bootstrap +strip]",
                "mpc": "[libs=static]",
                "gmp": "[libs=static]",
                "mpfr": "[libs=static]",
                "zstd": "[libs=static]",
                "zlib": "[~shared]"
            }
        }
        bootstrap_spec = raw["bootstrap"]["specs"][0]
        bootstrap["specs"] = [bootstrap_spec + " languages=c,c++", "squashfs default_compression=zstd"]
        compilers["bootstrap"] = bootstrap

        gcc = {}
        gcc["packages"] = {
            "external": ["perl", "m4", "autoconf", "automake", "libtool", "gawk", "python"],
            "variants": {
                "gcc": "[build_type=Release +strip]",
                "mpc": "[libs=static]",
                "gmp": "[libs=static]",
                "mpfr": "[libs=static]",
                "zstd": "[libs=static]",
                "zlib": "[~shared]"
            }
        }
        gcc["specs"] = raw["gcc"]["specs"]
        gcc["requires"] = bootstrap_spec
        compilers["gcc"] = gcc
        if "llvm" in raw:
            llvm = {}
            llvm["packages"] = False
            llvm["specs"] = []
            for spec in raw["llvm"]["specs"]:
                if spec.startswith("nvhpc"):
                    llvm["specs"].append(spec + "~mpi~blas~lapack")
                if spec.startswith("llvm"):
                    llvm["specs"].append(spec + " +clang targets=x86 ~gold ^ninja@kitware")
            llvm["requires"] = raw["llvm"]["requires"]
            compilers["llvm"] = llvm

        self.compilers = compilers


    def generate_compilers(self):
        files = {}

        template_path = os.path.join(tool_prefix, 'templates')
        env = jinja2.Environment(
                loader = jinja2.FileSystemLoader(template_path),
                trim_blocks=True, lstrip_blocks=True)

        makefile_template = env.get_template('Makefile.compilers')
        files['makefile'] = makefile_template.render(compilers=self.compilers)

        # generate compilers/<compiler>/spack.yaml
        files['config'] = {}
        for compiler, config in self.compilers.items():
            spack_yaml_template = env.get_template('compilers.'+compiler+'.spack.yaml')
            files['config'][compiler] = spack_yaml_template.render(config=config)

        return files

    def generate_packages(self):
        files = {}

        template_path = os.path.join(tool_prefix, 'templates')
        jenv = jinja2.Environment(
                loader = jinja2.FileSystemLoader(template_path),
                trim_blocks=True, lstrip_blocks=True)

        makefile_template = jenv.get_template('Makefile.packages')
        files['makefile'] = makefile_template.render(compilers=self.compilers, environments=self.packages)

        files['config'] = {}
        for env, config in self.packages.items():
            spack_yaml_template = jenv.get_template('packages.spack.yaml')
            files['config'][env] = spack_yaml_template.render(config=config)

        return files

class Build:
    # The path where the project is to be created
    path = ''

    def __init__(self, args):
        path = args.build
        if not os.path.isabs(path):
            path = os.path.join(os.path.abspath(os.path.curdir), path)
        if os.path.exists(path):
            if not os.path.isdir(path):
                raise IOError('build path is not a directory')

        self.path = path

    def generate(self, recipe):
        # make the paths
        store_path = os.path.join(self.path, 'store')
        tmp_path = os.path.join(self.path, 'tmp')

        os.makedirs(self.path, exist_ok=True)
        os.makedirs(store_path, exist_ok=True)
        os.makedirs(tmp_path, exist_ok=True)

        # check out the version of spack

        spack = recipe.config['spack']
        spack_path = os.path.join(self.path, 'spack')

        # clone the repository if the repos has not already been checked out
        if not os.path.isdir(os.path.join(spack_path, '.git')):
            return_code = subprocess.call(["git", "clone", spack['repo'], spack_path], shell=False)
            if return_code != 0:
                raise RuntimeError('error cloning the repository {0}'.format(spack['repo']))
        return_code = subprocess.call(["git", "-C", spack_path, "checkout", spack['commit']], shell=False)
        if return_code != 0:
            raise RuntimeError('unable to change to the requested commit {0}'.format(spack['commit']))

        # patch in the cray-mpich-binary package to spack
        mpi_pkg_path = os.path.join(spack_path, 'var/spack/repos/builtin/packages/cray-mpich-binary')
        mpi_pkg_src = os.path.join(tool_prefix, 'etc/cray-mpich-binary-package.py')
        os.makedirs(mpi_pkg_path, exist_ok=True)
        shutil.copy(mpi_pkg_src, os.path.join(mpi_pkg_path, 'package.py'))

        # load the jinja templating environment
        template_path = os.path.join(tool_prefix, 'templates')
        env = jinja2.Environment(
                loader = jinja2.FileSystemLoader(template_path),
                trim_blocks=True, lstrip_blocks=True)

        # generate top level makefiles
        make_user_template = env.get_template('Make.user')
        with open(os.path.join(self.path, 'Make.user'), 'w') as f:
            f.write(make_user_template.render(build_path=self.path, store=recipe.config['store'], verbose=False))
            f.write('\n')
            f.close()

        etc_path = os.path.join(tool_prefix, 'etc')
        for f in ['Makefile', 'Make.inc', 'bwrap-mutable-root.sh']:
            shutil.copy2(os.path.join(etc_path, f), os.path.join(self.path, f))

        # generate the system configuration
        config_path = os.path.join(self.path, 'config')
        os.makedirs(config_path, exist_ok=True)
        system = recipe.config['system']
        system_configs_path = os.path.join(os.path.join(os.path.join(tool_prefix, 'share'), 'cluster-config'), system)
        if not os.path.isdir(system_configs_path):
            raise RuntimeError('the system name {0} does not match any known system configuration'.format(system))

        for f in os.listdir(system_configs_path):
            # construct full file path
            src = os.path.join(system_configs_path, f)
            dst = os.path.join(config_path, f)
            # copy only files
            if os.path.isfile(src):
                shutil.copy(src, dst)

        # generate the makefile and spack.yaml files that describe the compilers
        compilers = recipe.generate_compilers()
        compiler_path = os.path.join(self.path, 'compilers')
        os.makedirs(compiler_path, exist_ok=True)
        with open(os.path.join(compiler_path, 'Makefile'), mode='w') as f:
            f.write(compilers['makefile'])
            f.close()

        for name, yaml in compilers['config'].items():
            compiler_config_path = os.path.join(compiler_path, name)
            os.makedirs(compiler_config_path, exist_ok=True)
            with open(os.path.join(compiler_config_path, 'spack.yaml'), mode='w') as f:
                f.write(yaml)
                f.close()

        # generate the makefile and spack.yaml files that describe the packages
        packages = recipe.generate_packages()
        package_path = os.path.join(self.path, 'packages')
        os.makedirs(package_path, exist_ok=True)
        with open(os.path.join(package_path, 'Makefile'), mode='w') as f:
            f.write(packages['makefile'])
            f.close()

        for name, yaml in packages['config'].items():
            pkg_config_path = os.path.join(package_path, name)
            os.makedirs(pkg_config_path, exist_ok=True)
            with open(os.path.join(pkg_config_path, 'spack.yaml'), mode='w') as f:
                f.write(yaml)
                f.close()

        #
        # generate the makefile that generates the configuration for the spack installation
        #
        make_config_template = env.get_template('Makefile.generate-config')
        generate_config_path = os.path.join(self.path, 'generate-config')
        os.makedirs(generate_config_path, exist_ok=True)

        # write the modules.yaml file
        modules_yaml=recipe.generate_modules()
        with open(os.path.join(generate_config_path, 'modules.yaml'), 'w') as f:
            f.write(modules_yaml)
            f.close()

        # write the Makefile
        compiler_names=[x for x in recipe.compilers.keys() if x!="bootstrap"]
        with open(os.path.join(generate_config_path, 'Makefile'), 'w') as f:
            f.write(make_config_template.render(
                build_path=self.path,
                compilers=compiler_names,
                verbose=False))
            f.close()

def main(prefix):
    global tool_prefix
    tool_prefix = prefix

    try:
        parser = make_argparser()
        args = parser.parse_args()
        recipe = Recipe(args)
        build = Build(args)
        build.generate(recipe)

        return 0
    except Exception as e:
        print(str(e))
        if args.debug:
            raise
        return 1

