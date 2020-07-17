import glob
import io
import os
import os.path
import platform
import pytest
import re
import shutil
import subprocess
import sys
import difflib

from pyutilib.misc.pyyaml_util import compare_repn as compare_xml
from pyutilib.misc.xmltodict import parse as parse_xml

python_interpreter = sys.executable.replace('\\', '/')  # use forward slash on windows as well
env = os.environ
env['GCOVR'] = python_interpreter + ' -m gcovr'

basedir = os.path.split(os.path.abspath(__file__))[0]

skip_clean = None

RE_DECIMAL = re.compile(r'(\d+\.\d+)')

RE_TXT_WHITESPACE = re.compile(r'[ ]+$', flags=re.MULTILINE)

RE_XML_ATTRS = re.compile(r'(timestamp)="[^"]*"')
RE_XML_GCOVR_VERSION = re.compile(r'version="gcovr [^"]+"')

RE_HTML_ATTRS = re.compile('((timestamp)|(version))="[^"]*"')
RE_HTML_FOOTER_VERSION = re.compile(
    r'(Generated by: <a [^>]+>GCOVR \(Version) (?:3|4).[\w.-]+(\)</a>)')
RE_HTML_HEADER_DATE = re.compile(
    r'(<td)>\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d<(/td>)')


def scrub_txt(contents):
    return RE_TXT_WHITESPACE.sub('', contents)


def scrub_csv(contents):
    contents = contents.replace("\r", "")
    contents = contents.replace("\n\n", "\n")
    # Replace windows file separator for html reports generated in Windows
    contents = contents.replace('\\', '/')
    return contents


def scrub_xml(contents):
    contents = RE_DECIMAL.sub(lambda m: str(round(float(m.group(1)), 5)), contents)
    contents = RE_XML_ATTRS.sub(r'\1=""', contents)
    contents = RE_XML_GCOVR_VERSION.sub('version=""', contents)
    contents = contents.replace("\r", "")
    return contents


def scrub_html(contents):
    contents = RE_HTML_ATTRS.sub('\\1=""', contents)
    contents = RE_HTML_FOOTER_VERSION.sub("\\1 4.x\\2", contents)
    contents = RE_HTML_HEADER_DATE.sub("\\1>0000-00-00 00:00:00<\\2", contents)
    contents = contents.replace("\r", "")
    # Replace windows file separator for html reports generated in Windows
    contents = contents.replace('\\', '/')
    return contents


def findtests(basedir):
    for f in os.listdir(basedir):
        if not os.path.isdir(os.path.join(basedir, f)):
            continue
        if f.startswith('.'):
            continue
        if 'pycache' in f:
            continue
        yield f


def assert_xml_equals(coverage, reference):
    coverage_repn = parse_xml(coverage)
    reference_repn = parse_xml(reference)
    compare_xml(reference_repn, coverage_repn, tolerance=1e-4, exact=True)


def run(cmd, cwd=None):
    print("STDOUT - START", str(cmd))
    returncode = subprocess.call(cmd, stderr=subprocess.STDOUT, env=env, cwd=cwd)
    print("STDOUT - END")
    return returncode == 0


def find_reference_files(output_pattern):
    for pattern in output_pattern:
        for reference in glob.glob("reference/" + pattern):
            coverage = os.path.basename(reference)
            yield coverage, reference


@pytest.fixture(scope='module')
def compiled(request, name):
    path = os.path.join(basedir, name)
    assert run(['make', 'clean'], cwd=path)
    assert run(['make', 'all'], cwd=path)
    yield name
    if not skip_clean:
        assert run(['make', 'clean'], cwd=path)


KNOWN_FORMATS = ['txt', 'xml', 'html', 'sonarqube', 'json', 'csv']


def pytest_generate_tests(metafunc):
    """generate a list of all available integration tests."""

    is_windows = platform.system() == 'Windows'

    global skip_clean
    skip_clean = metafunc.config.getoption("skip_clean")
    generate_reference = metafunc.config.getoption("generate_reference")
    update_reference = metafunc.config.getoption("update_reference")

    collected_params = []

    for name in findtests(basedir):
        targets = parse_makefile_for_available_targets(
            os.path.join(basedir, name, 'Makefile'))

        # check that the "run" target lists no unknown formats
        target_run = targets.get('run', set())
        unknown_formats = target_run.difference(KNOWN_FORMATS)
        if unknown_formats:
            raise ValueError("{}/Makefile target 'run' references unknown format {}".format(
                name, unknown_formats))

        # check that all "run" targets are actually available
        unresolved_prereqs = target_run.difference(targets)
        if unresolved_prereqs:
            raise ValueError("{}/Makefile target 'run' has unresolved prerequisite {}".format(
                name, unresolved_prereqs))

        # check that all available known formats are also listed in the "run" target
        unreferenced_formats = set(KNOWN_FORMATS).intersection(targets).difference(target_run)
        if unreferenced_formats:
            raise ValueError("{}/Makefile target 'run' doesn't reference available target {}".format(
                name, unreferenced_formats))

        for format in KNOWN_FORMATS:

            # only test formats where the Makefile provides a target
            if format not in targets:
                continue

            needs_symlinks = any([
                name == 'linked' and format == 'html',
                name == 'filter-relative-lib',
            ])

            marks = [
                pytest.mark.xfail(
                    needs_symlinks and is_windows,
                    reason="have yet to figure out symlinks on Windows"),
                pytest.mark.xfail(
                    name == 'exclude-throw-branches' and format == 'html' and is_windows,
                    reason="branch coverage details seem to be platform-dependent"),
                pytest.mark.xfail(
                    name == 'rounding' and is_windows,
                    reason="branch coverage seem to be platform-dependent")
            ]

            collected_params.append(pytest.param(
                name, format, targets, generate_reference, update_reference,
                marks=marks,
                id='-'.join([name, format]),
            ))

    metafunc.parametrize(
        'name, format, available_targets, generate_reference, update_reference', collected_params,
        indirect=False,
        scope='module')


def parse_makefile_for_available_targets(path):
    targets = {}
    with open(path) as makefile:
        for line in makefile:
            m = re.match(r'^(\w[\w -]*):([\s\w.-]*)$', line)
            if m:
                deps = m.group(2).split()
                for target in m.group(1).split():
                    targets.setdefault(target, set()).update(deps)
    return targets


SCRUBBERS = dict(
    txt=scrub_txt,
    xml=scrub_xml,
    html=scrub_html,
    sonarqube=scrub_xml,
    json=lambda x: x,
    csv=scrub_csv)

OUTPUT_PATTERN = dict(
    txt=['coverage.txt'],
    xml=['coverage.xml'],
    html=['coverage*.html', 'coverage.css'],
    sonarqube=['sonarqube.xml'],
    json=['coverage*.json'],
    csv=['coverage.csv'])

ASSERT_EQUALS = dict(
    xml=assert_xml_equals,
    sonarqube=assert_xml_equals)


def test_build(compiled, format, available_targets, generate_reference, update_reference):
    name = compiled
    scrub = SCRUBBERS[format]
    output_pattern = OUTPUT_PATTERN[format]
    assert_equals = ASSERT_EQUALS.get(format, None)

    encoding = 'utf8'
    if format == 'html' and name.startswith('html-encoding-'):
        encoding = re.match('^html-encoding-(.*)$', name).group(1)

    os.chdir(os.path.join(basedir, name))
    assert run(["make", format])

    if generate_reference:  # pragma: no cover
        for pattern in output_pattern:
            for generated_file in glob.glob(pattern):
                reference_file = os.path.join('reference', generated_file)
                if os.path.isfile(reference_file):
                    continue
                else:
                    try:
                        os.makedirs('reference')
                    except FileExistsError:
                        # directory already exists
                        pass

                    print('copying %s to %s' % (generated_file, reference_file))
                    shutil.copyfile(generated_file, reference_file)

    for coverage_file, reference_file in find_reference_files(output_pattern):
        with io.open(coverage_file, encoding=encoding) as f:
            coverage_raw = f.read()
            coverage = scrub(coverage_raw)
        with io.open(reference_file, encoding=encoding) as f:
            reference = scrub(f.read())

        if assert_equals is not None:
            assert_equals(coverage, reference)
        else:
            diff_out = list(difflib.unified_diff(reference.splitlines(keepends=True), coverage.splitlines(keepends=True), fromfile=reference_file, tofile=coverage_file))
            diff_is_empty = len(diff_out) == 0
            if not diff_is_empty and update_reference:  # pragma: no cover
                with io.open(reference_file, mode="w", encoding=encoding) as f:
                    f.write(coverage_raw)
            assert diff_is_empty, "Unified diff output:\n" + "".join(diff_out)

    # some tests require additional cleanup after each test
    if 'clean-each' in available_targets:
        assert run(['make', 'clean-each'])

    os.chdir(basedir)
