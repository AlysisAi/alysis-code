# Legacy ACL caller review — 4 October 2026

The legacy ACL privilege-escalation precondition is absent within the reviewed
application-controlled Docker execution boundary. This is a scoped applicability
decision, not a claim that coreutils migrated its calls or that libacl's old path
APIs stopped following symlinks. It adds no scanner exception. Privileged manual
administration, custom Docker arguments, unrelated host processes, and host-kernel
vulnerabilities are outside this conclusion.

## Caller and authority

The authenticated coreutils 9.12-r3 binary imports `acl_set_file` and
`acl_delete_def_file`; its pinned gnulib still has path-based fallback calls.
[Upstream's announcement](https://www.openwall.com/lists/oss-security/2026/06/29/1)
explains that a privileged caller processing attacker-controlled paths can redirect
these operations through symlinks. Library 2.4.0 adds safer APIs but preserves
legacy semantics. The full filesystem inventory found this one dynamic caller on
each image; coreutils has no setuid/setgid mode.

The application's `_build_docker_argv` always drops **all** Linux capabilities and
sets **no-new-privileges**. On POSIX it uses the host UID/GID, including zero; other
platforms use the image's `alysis` user. Every child command starts with the same
authority, and setuid execution cannot increase it. The only host read/write bind
is the selected workspace. Existing protected metadata receives read-only mounts
for arbitrary shell commands. A symlink resolves in the container mount namespace;
it cannot make an unmounted host path available or turn a read-only mount writable.

Even UID zero without CAP_FOWNER cannot change another UID's ACL merely because
file content is world-writable. UID zero can change its own container files;
the writable image root is not a security boundary against that identity. Within
the allowed workspace, commands already have the intentionally granted write and
owner authority. A legacy ACL operation adds no new authority across the reviewed
host/mount/identity boundaries. No assumption of a read-only root filesystem or
configured CPU/memory limits is needed for this particular conclusion.

## Actual-profile integration evidence

All six jobs in private run
[37215707465](https://github.com/AlysisAi/alysis-code-internal/actions/runs/37215707465)
passed at `a998c0b3fec3200e09594924bc126e60afdf2419`. Each test called the actual
application argument builder, using writable rootfs, no optional resource limits,
network off, metadata protection on, and UIDs 1000 and zero. The image IDs match
the complete filesystem inventories and HIGH/CRITICAL scans, all of which passed.

The test deliberately follows directory symlinks rather than relying on winning
a timing race. Across the six images and two identities it checked 144 legacy ACL
operations: setting access ACLs, setting default directory ACLs, and deleting
default ACLs. For each operation:

- The owned writable target succeeds, confirming that the old API was exercised.
- A different UID's target returns EPERM, even though content writes are allowed.
- A protected metadata target returns EROFS, including through the symlink.
- The actual host path outside the mount returns ENOENT.

The test also checks zero effective/permitted/bounding/inheritable/ambient
capabilities, no-new-privileges, unchanged identity after a setuid helper, failed
coreutils preservation copies into protected metadata, and unchanged metadata ACLs
and host files. Fixture preparation uses a separate container with narrowly added
CHOWN/FOWNER/FSETID capabilities solely for disposable files; those capabilities
are absent from the tested application profile.

Checker SHA-256:
`173f829c936bb43b056835f25edef1cdf011598cb2d680eec2eee12b2f0e7790`.
Application policy source SHA-256:
`5d3b464aecffca556c04191bd50cef4f0de39958cb03bea099b0b94c36c5e502`.
Receipts independently bind these Linux Git bytes, source commit, image ID, and
scan. The evidence archive is `acl-boundary-a998c0b3.zip`, SHA-256
`edae6632021f97a3fb40aa019af3788c15d7601878406aa8050acbb455ee4596`.

## Conditions for production adoption

Keep these checks in production qualification. Reopen the decision if capability
dropping, no-new-privileges, mounts, user selection, or privileged helpers change.
Direct/manual use of the image must preserve the same controls; running it with
extra privileges or broader host mounts is not covered. Explicitly disabling
metadata protection grants that write scope and is not evidence of protection.
Resource exhaustion and host-kernel patching remain separate operational concerns.
These results do not alone authorize image or Marketplace promotion.
