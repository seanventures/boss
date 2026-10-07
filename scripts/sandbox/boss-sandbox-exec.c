/*
 * boss-sandbox-exec: the ONLY privileged piece of the code-task sandbox (IDENTITY.md §7.6).
 *
 * A code task's agent, its commands and its tests run as a per-run UNPRIVILEGED uid from a fixed
 * pool, never as `boss` (the API/worker uid). A different uid is what makes the API's
 * /proc/<pid>/environ, the identity key volume, other runs' directories and shim sockets
 * unreachable, and what the network rules (scripts/sandbox/entrypoint.sh) key on.
 *
 * Installed root:boss 0750 with file capabilities cap_setuid,cap_setgid,cap_chown,cap_fowner+ep,
 * so only the `boss` group can execute it, and it can do exactly four things:
 *
 *   run   <uid> -- <argv...>   fork; the child starts a new session, drops to <uid>:<uid> with no
 *                              supplementary groups, no_new_privs, umask 007, parent-death SIGKILL,
 *                              and execs argv. The parent relays SIGTERM/SIGINT/SIGHUP to the
 *                              child's process group (as <uid>), SIGUSR1 as SIGKILL; when the child
 *                              exits it SIGKILLs whatever is left in that group, and exits with the
 *                              child's status.
 *   kill  <uid>                every process of <uid> gets SIGKILL (run end, revoke, uid reuse).
 *   adopt <uid> <dir>          <dir> must be a direct child of RUN_ROOT, a real directory owned by
 *                              the caller: it becomes <uid>:boss 2770 (the run and BOSS only).
 *   sock  <uid> <path>         <path> must be a socket directly in SHIM_ROOT owned by the caller:
 *                              it becomes <uid>:<uid> 0600 (that run's agent only).
 *
 * <uid> must be inside [UID_BASE, UID_BASE + UID_COUNT). Nothing here takes a uid outside the
 * pool, follows a symlink, or touches a path outside its two roots.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef UID_BASE
#define UID_BASE 61000
#endif
#ifndef UID_COUNT
#define UID_COUNT 1000
#endif
#ifndef RUN_ROOT
#define RUN_ROOT "/var/lib/boss/sandboxes"
#endif
#ifndef SHIM_ROOT
#define SHIM_ROOT "/run/boss-shim"
#endif
#define GRACE_SECONDS 3

static void die(const char *what) {
    fprintf(stderr, "boss-sandbox-exec: %s: %s\n", what, strerror(errno));
    _exit(126);
}

static void refuse(const char *what) {
    fprintf(stderr, "boss-sandbox-exec: refused: %s\n", what);
    _exit(125);
}

static uid_t parse_uid(const char *s) {
    char *end = NULL;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || !end || *end || v < UID_BASE || v >= (long)UID_BASE + UID_COUNT) refuse("uid outside the sandbox pool");
    return (uid_t)v;
}

/* Become <uid> for good: no groups, no way back, no new privileges. */
static void become(uid_t uid) {
    if (setgroups(0, NULL) != 0) die("setgroups");
    if (setresgid(uid, uid, uid) != 0) die("setresgid");
    if (setresuid(uid, uid, uid) != 0) die("setresuid");
    if (getuid() != uid || geteuid() != uid || getgid() != uid) refuse("identity did not change");
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) die("no_new_privs");
}

static void close_fds_from(int lowest) {
    DIR *d = opendir("/proc/self/fd");
    if (!d) {
        for (int fd = lowest; fd < 4096; fd++) close(fd);
        return;
    }
    int dfd = dirfd(d);
    struct dirent *e;
    int fds[4096];
    int n = 0;
    while ((e = readdir(d)) != NULL && n < 4096) {
        int fd = atoi(e->d_name);
        if (fd >= lowest && fd != dfd) fds[n++] = fd;
    }
    closedir(d);
    for (int i = 0; i < n; i++) close(fds[i]);
}

/* Signal as <uid> (a privileged parent cannot signal another uid without CAP_KILL, and is not
 * given it): fork, become <uid>, signal, exit. target<0 is a process group, -1 is "all of uid". */
static void signal_as(uid_t uid, pid_t target, int sig) {
    pid_t p = fork();
    if (p < 0) return;
    if (p == 0) {
        become(uid);
        if (target == -1) {
            for (int i = 0; i < 20; i++) {
                if (kill(-1, sig) != 0 && errno == ESRCH) break;
                usleep(50 * 1000);
            }
        } else {
            kill(target, sig);
        }
        _exit(0);
    }
    int st;
    while (waitpid(p, &st, 0) < 0 && errno == EINTR) {}
}

static volatile sig_atomic_t pending_sig = 0;
static volatile sig_atomic_t grace_over = 0;
static void on_signal(int sig) { pending_sig = sig; }
static void on_alarm(int sig) { (void)sig; grace_over = 1; }

static int op_run(uid_t uid, char **argv) {
    pid_t parent = getpid();
    pid_t child = fork();
    if (child < 0) die("fork");
    if (child == 0) {
        if (setsid() < 0) die("setsid");
        become(uid);
        if (prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0) die("pdeathsig");
        if (getppid() != parent) _exit(137);     /* the relay is already gone */
        umask(007);
        close_fds_from(3);
        execvp(argv[0], argv);
        die("exec");
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);
    sigaction(SIGUSR1, &sa, NULL);
    struct sigaction al;
    memset(&al, 0, sizeof al);
    al.sa_handler = on_alarm;
    sigaction(SIGALRM, &al, NULL);
    int status = 0;
    int relayed = 0;
    for (;;) {
        pid_t w = waitpid(child, &status, 0);
        if (w == child) break;
        if (w < 0 && errno != EINTR) die("waitpid");
        if (pending_sig) {
            int s = pending_sig;
            pending_sig = 0;
            if (s == SIGUSR1) {
                signal_as(uid, -child, SIGKILL);
            } else {
                signal_as(uid, -child, s);
                if (!relayed) {
                    relayed = 1;
                    alarm(GRACE_SECONDS);     /* then SIGKILL, whatever the group did */
                }
            }
        }
        if (grace_over) {
            grace_over = 0;
            signal_as(uid, -child, SIGKILL);
        }
    }
    /* The command ending ends its group (a dev server it left behind, a watcher). */
    signal_as(uid, -child, SIGKILL);
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}

/* `name` must be one plain path component. */
static int plain_component(const char *name) {
    if (!name[0] || strchr(name, '/') || strcmp(name, ".") == 0 || strcmp(name, "..") == 0) return 0;
    return 1;
}

static const char *child_of(const char *root, const char *path) {
    size_t n = strlen(root);
    if (strncmp(path, root, n) != 0 || path[n] != '/') return NULL;
    const char *name = path + n + 1;
    return plain_component(name) ? name : NULL;
}

static int op_adopt(uid_t uid, const char *path, int want_socket) {
    const char *root = want_socket ? SHIM_ROOT : RUN_ROOT;
    const char *name = child_of(root, path);
    if (!name) refuse("path is not directly inside the sandbox root");
    int rootfd = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (rootfd < 0) die("open root");
    struct stat rs;
    if (fstat(rootfd, &rs) != 0) die("stat root");
    if (rs.st_uid != getuid() || (rs.st_mode & 022)) refuse("root is not the caller's or is group/world-writable");
    struct stat st;
    if (fstatat(rootfd, name, &st, AT_SYMLINK_NOFOLLOW) != 0) die("stat");
    if (st.st_uid != getuid()) refuse("not owned by the caller");
    if (want_socket) {
        if (!S_ISSOCK(st.st_mode)) refuse("not a socket");
        if (fchownat(rootfd, name, uid, uid, AT_SYMLINK_NOFOLLOW) != 0) die("chown");
        if (fchmodat(rootfd, name, 0600, 0) != 0) die("chmod");
    } else {
        if (!S_ISDIR(st.st_mode)) refuse("not a directory");
        int fd = openat(rootfd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd < 0) die("open dir");
        if (fchown(fd, uid, getgid()) != 0) die("chown");
        if (fchmod(fd, 02770) != 0) die("chmod");
        close(fd);
    }
    close(rootfd);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 3) refuse("usage: run <uid> -- argv... | kill <uid> | adopt <uid> <dir> | sock <uid> <path>");
    if (getuid() < UID_BASE + UID_COUNT && getuid() >= UID_BASE) refuse("a sandbox uid may not use this");
    uid_t uid = parse_uid(argv[2]);
    if (strcmp(argv[1], "run") == 0) {
        if (argc < 5 || strcmp(argv[3], "--") != 0) refuse("run <uid> -- argv...");
        return op_run(uid, argv + 4);
    }
    if (strcmp(argv[1], "kill") == 0) {
        signal_as(uid, -1, SIGKILL);
        return 0;
    }
    if (strcmp(argv[1], "adopt") == 0 && argc == 4) return op_adopt(uid, argv[3], 0);
    if (strcmp(argv[1], "sock") == 0 && argc == 4) return op_adopt(uid, argv[3], 1);
    refuse("unknown operation");
    return 125;
}
