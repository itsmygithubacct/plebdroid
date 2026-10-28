/* Android PIE launcher for the Kilix Python desktop.
 * Installed as libplebdroid_python.so and executed from a PTY.
 * PLEBDROID_RUNTIME_ROOT is the absolute Python prefix (on-device
 * runtime/python) and must contain lib/python3.14.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define PLEBDROID_RUNTIME_ENV "PLEBDROID_RUNTIME_ROOT"
#define PLEBDROID_STDLIB_REL "lib/python3.14"
#define PLEBDROID_SITE_REL "lib/python3.14/site-packages"
#define PLEBDROID_SIBLING_SITE "python-site"

static void set_message(char *message, size_t message_len, const char *text)
{
    if (message == NULL || message_len == 0) {
        return;
    }
    snprintf(message, message_len, "%s", text);
}

int plebdroid_normalize_root(const char *root, char *normalized,
                             size_t normalized_len, char *message,
                             size_t message_len)
{
    size_t length;

    if (root == NULL || root[0] == '\0') {
        set_message(message, message_len,
                    PLEBDROID_RUNTIME_ENV
                    " is required and must be the absolute runtime/python prefix");
        return 2;
    }
    if (root[0] != '/') {
        set_message(message, message_len,
                    PLEBDROID_RUNTIME_ENV " must be an absolute path");
        return 2;
    }
    length = strlen(root);
    while (length > 1 && root[length - 1] == '/') {
        length--;
    }
    if (length + 1 > normalized_len) {
        set_message(message, message_len,
                    PLEBDROID_RUNTIME_ENV " is too long");
        return 2;
    }
    memmove(normalized, root, length);
    normalized[length] = '\0';
    return 0;
}

int plebdroid_check_runtime_root(const char *root, char *message,
                                 size_t message_len)
{
    char normalized[PATH_MAX];
    char stdlib[PATH_MAX];
    struct stat info;
    int status;
    int written;

    status = plebdroid_normalize_root(root, normalized, sizeof normalized,
                                      message, message_len);
    if (status != 0) {
        return status;
    }
    written = snprintf(stdlib, sizeof stdlib, "%s/%s", normalized,
                       PLEBDROID_STDLIB_REL);
    if (written < 0 || (size_t)written >= sizeof stdlib) {
        set_message(message, message_len,
                    PLEBDROID_RUNTIME_ENV " is too long");
        return 2;
    }
    if (stat(stdlib, &info) != 0 || !S_ISDIR(info.st_mode)) {
        if (message != NULL && message_len > 0) {
            snprintf(message, message_len,
                     "Python stdlib directory not found: %s", stdlib);
        }
        return 2;
    }
    return 0;
}

/* Copy dirname(root)/python-site when that directory exists. */
int plebdroid_sibling_site(const char *normalized_root, char *out,
                           size_t out_len)
{
    const char *slash;
    size_t parent_len;
    struct stat info;
    int written;

    if (normalized_root == NULL || out == NULL || out_len == 0) {
        return 0;
    }
    slash = strrchr(normalized_root, '/');
    if (slash == NULL) {
        return 0;
    }
    parent_len = (size_t)(slash - normalized_root);
    if (parent_len == 0) {
        written = snprintf(out, out_len, "/%s", PLEBDROID_SIBLING_SITE);
    } else {
        written = snprintf(out, out_len, "%.*s/%s", (int)parent_len,
                           normalized_root, PLEBDROID_SIBLING_SITE);
    }
    if (written < 0 || (size_t)written >= out_len) {
        return 0;
    }
    if (stat(out, &info) != 0 || !S_ISDIR(info.st_mode)) {
        out[0] = '\0';
        return 0;
    }
    return 1;
}

/* The engine execs this PIE with its absolute path as argv[0]. Under a
 * native bridge, /proc/self/exe can name the translator instead, so it is
 * only the fallback when argv[0] is not an absolute executable file.
 */
int plebdroid_program_path(const char *argv0, char *out, size_t out_len)
{
    struct stat info;
    ssize_t length;

    if (out == NULL || out_len == 0) {
        return -1;
    }
    if (argv0 != NULL && argv0[0] == '/' && strlen(argv0) < out_len &&
        stat(argv0, &info) == 0 && S_ISREG(info.st_mode) &&
        access(argv0, X_OK) == 0) {
        memcpy(out, argv0, strlen(argv0) + 1);
        return 0;
    }
    length = readlink("/proc/self/exe", out, out_len - 1);
    if (length < 0) {
        return -1;
    }
    if ((size_t)length >= out_len - 1) {
        errno = ENAMETOOLONG;
        return -1;
    }
    out[length] = '\0';
    return 0;
}

#ifdef PLEBDROID_HOST_SELFTEST

static int fail(const char *name)
{
    fprintf(stderr, "selftest failed: %s\n", name);
    return 1;
}

int main(void)
{
    char message[1024];
    char normalized[PATH_MAX];
    char sibling[PATH_MAX];
    char templ[] = "/tmp/plebdroid-python-app-XXXXXX";
    char *root = mkdtemp(templ);
    char stdlib[PATH_MAX];
    char site[PATH_MAX];
    int failures = 0;

    if (root == NULL) {
        perror("mkdtemp");
        return 1;
    }
    if (plebdroid_check_runtime_root(NULL, message, sizeof message) == 0 ||
        strstr(message, "required") == NULL) {
        failures += fail("missing env");
    }
    if (plebdroid_check_runtime_root("runtime/python", message,
                                     sizeof message) == 0 ||
        strstr(message, "absolute") == NULL) {
        failures += fail("relative root");
    }
    snprintf(stdlib, sizeof stdlib, "%s/empty", root);
    if (mkdir(stdlib, 0700) != 0) {
        perror("mkdir");
        return 1;
    }
    if (plebdroid_check_runtime_root(stdlib, message, sizeof message) == 0 ||
        strstr(message, "not found") == NULL) {
        failures += fail("missing stdlib");
    }
    snprintf(stdlib, sizeof stdlib, "%s/python/lib/python3.14", root);
    snprintf(site, sizeof site, "%s/python/lib/python3.14/encodings", root);
    if (mkdir(root, 0700) != 0 && errno != EEXIST) {
        perror("mkdir root");
        return 1;
    }
    {
        char python_dir[PATH_MAX];
        char lib_dir[PATH_MAX];
        snprintf(python_dir, sizeof python_dir, "%s/python", root);
        snprintf(lib_dir, sizeof lib_dir, "%s/python/lib", root);
        if (mkdir(python_dir, 0700) != 0 || mkdir(lib_dir, 0700) != 0 ||
            mkdir(stdlib, 0700) != 0 || mkdir(site, 0700) != 0) {
            perror("mkdir stdlib");
            return 1;
        }
    }
    snprintf(normalized, sizeof normalized, "%s/python//", root);
    if (plebdroid_check_runtime_root(normalized, message, sizeof message) != 0) {
        fprintf(stderr, "stdlib check message: %s\n", message);
        failures += fail("valid root");
    }
    {
        char expected[PATH_MAX];
        snprintf(expected, sizeof expected, "%s/python", root);
        if (plebdroid_normalize_root(normalized, sibling, sizeof sibling,
                                     message, sizeof message) != 0 ||
            strcmp(sibling, expected) != 0) {
            failures += fail("strip trailing slash");
        }
    }
    {
        char program[PATH_MAX];
        char self[PATH_MAX];
        char tool[PATH_MAX];
        ssize_t self_len = readlink("/proc/self/exe", self, sizeof self - 1);
        FILE *handle;

        if (self_len < 0) {
            perror("readlink");
            return 1;
        }
        self[self_len] = '\0';
        snprintf(tool, sizeof tool, "%s/tool", root);
        handle = fopen(tool, "w");
        if (handle == NULL || fclose(handle) != 0) {
            perror("tool");
            return 1;
        }
        if (plebdroid_program_path(tool, program, sizeof program) != 0 ||
            strcmp(program, self) != 0) {
            failures += fail("non-executable argv0 falls back");
        }
        if (chmod(tool, 0700) != 0) {
            perror("chmod");
            return 1;
        }
        if (plebdroid_program_path(tool, program, sizeof program) != 0 ||
            strcmp(program, tool) != 0) {
            failures += fail("absolute argv0");
        }
        if (plebdroid_program_path("libplebdroid_python.so", program,
                                   sizeof program) != 0 ||
            strcmp(program, self) != 0) {
            failures += fail("relative argv0 falls back");
        }
        if (plebdroid_program_path(NULL, program, sizeof program) != 0 ||
            strcmp(program, self) != 0) {
            failures += fail("missing argv0 falls back");
        }
        if (plebdroid_program_path(root, program, sizeof program) != 0 ||
            strcmp(program, self) != 0) {
            failures += fail("directory argv0 falls back");
        }
        unlink(tool);
    }
    snprintf(site, sizeof site, "%s/python-site", root);
    if (mkdir(site, 0700) != 0) {
        perror("mkdir site");
        return 1;
    }
    if (plebdroid_normalize_root(normalized, normalized, sizeof normalized,
                                 message, sizeof message) != 0) {
        failures += fail("normalize");
    }
    if (!plebdroid_sibling_site(normalized, sibling, sizeof sibling) ||
        strcmp(sibling, site) != 0) {
        fprintf(stderr, "sibling was '%s'\n", sibling);
        failures += fail("sibling site");
    }
    if (plebdroid_sibling_site(stdlib, sibling, sizeof sibling)) {
        failures += fail("unexpected sibling");
    }
    {
        static const char *const created[] = {
            "python-site", "python/lib/python3.14/encodings",
            "python/lib/python3.14", "python/lib", "python", "empty",
        };
        char path[PATH_MAX];
        size_t index;

        for (index = 0; index < sizeof created / sizeof created[0]; index++) {
            snprintf(path, sizeof path, "%s/%s", root, created[index]);
            rmdir(path);
        }
        rmdir(root);
    }
    if (failures == 0) {
        printf("plebdroid python_app selftest ok\n");
    }
    return failures == 0 ? 0 : 1;
}

#else

#include <Python.h>

static void fail_config(PyConfig *config, PyStatus status)
{
    PyConfig_Clear(config);
    Py_ExitStatusException(status);
}

static int append_path(char *buffer, size_t buffer_len, const char *entry)
{
    size_t used = strlen(buffer);
    size_t entry_len = strlen(entry);
    int need_sep = used > 0;

    if (used + (need_sep ? 1 : 0) + entry_len + 1 > buffer_len) {
        return -1;
    }
    if (need_sep) {
        buffer[used++] = ':';
    }
    memcpy(buffer + used, entry, entry_len + 1);
    return 0;
}

int main(int argc, char **argv)
{
    const char *root_env = getenv(PLEBDROID_RUNTIME_ENV);
    char message[1024];
    char root[PATH_MAX];
    char exe[PATH_MAX];
    char site_packages[PATH_MAX];
    char sibling[PATH_MAX];
    char pythonpath[16384];
    const char *existing_pythonpath;
    int status_code;
    int written;
    PyStatus status;
    PyConfig config;
    char *fallback_argv[] = {"libplebdroid_python.so", NULL};

    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    status_code = plebdroid_normalize_root(root_env, root, sizeof root, message,
                                           sizeof message);
    if (status_code != 0) {
        fprintf(stderr, "plebdroid python: %s\n", message);
        return status_code;
    }
    status_code = plebdroid_check_runtime_root(root, message, sizeof message);
    if (status_code != 0) {
        fprintf(stderr, "plebdroid python: %s\n", message);
        return status_code;
    }

    if (plebdroid_program_path(argc > 0 && argv != NULL ? argv[0] : NULL, exe,
                               sizeof exe) != 0) {
        fprintf(stderr, "plebdroid python: cannot resolve the executable path: %s\n",
                strerror(errno));
        return 2;
    }

    written = snprintf(site_packages, sizeof site_packages, "%s/%s", root,
                       PLEBDROID_SITE_REL);
    if (written < 0 || (size_t)written >= sizeof site_packages) {
        fprintf(stderr, "plebdroid python: site-packages path is too long\n");
        return 2;
    }

    pythonpath[0] = '\0';
    existing_pythonpath = getenv("PYTHONPATH");
    if (existing_pythonpath != NULL && existing_pythonpath[0] != '\0' &&
        append_path(pythonpath, sizeof pythonpath, existing_pythonpath) != 0) {
        fprintf(stderr, "plebdroid python: PYTHONPATH is too long\n");
        return 2;
    }
    if (append_path(pythonpath, sizeof pythonpath, site_packages) != 0) {
        fprintf(stderr, "plebdroid python: module search path is too long\n");
        return 2;
    }
    if (plebdroid_sibling_site(root, sibling, sizeof sibling) &&
        append_path(pythonpath, sizeof pythonpath, sibling) != 0) {
        fprintf(stderr, "plebdroid python: module search path is too long\n");
        return 2;
    }

    if (argc < 1 || argv == NULL) {
        argc = 1;
        argv = fallback_argv;
    }

    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 1;
    config.configure_c_stdio = 1;
    config.buffered_stdio = 0;
    config.install_signal_handlers = 1;
    config.site_import = 1;
    config.user_site_directory = 0;
    config.pathconfig_warnings = 1;
    config.safe_path = 0;

    status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (PyStatus_Exception(status)) {
        fail_config(&config, status);
    }
    status = PyConfig_SetBytesString(&config, &config.home, root);
    if (PyStatus_Exception(status)) {
        fail_config(&config, status);
    }
    status = PyConfig_SetBytesString(&config, &config.program_name, exe);
    if (PyStatus_Exception(status)) {
        fail_config(&config, status);
    }
    if (pythonpath[0] != '\0') {
        status = PyConfig_SetBytesString(&config, &config.pythonpath_env,
                                         pythonpath);
        if (PyStatus_Exception(status)) {
            fail_config(&config, status);
        }
    }

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {
        fail_config(&config, status);
    }
    PyConfig_Clear(&config);
    return Py_RunMain();
}

#endif
