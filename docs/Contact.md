# Contact

> **This is Helix** — an evolution of Klipper. This page tells you where to
> ask questions, report bugs, and propose features. New to Helix? Start
> with the **[Helix overview](HELIX.md)**.

Helix has two places to turn, and picking the right one gets you help
faster:

- **Anything specific to Helix** — the trajectory motion path,
  pause-and-hold recovery, machine time, the authenticated network
  transport, signed firmware, or any `HELIX_`/`TRAJECTORY_` command —
  belongs on the **Helix project's own GitHub**:
  [github.com/jrlomas/klipper](https://github.com/jrlomas/klipper)
  ([issues](https://github.com/jrlomas/klipper/issues) ·
  [discussions](https://github.com/jrlomas/klipper/discussions)). Helix
  owns this code, so this is the only place a Helix-specific bug can
  actually be fixed.
- **General 3D-printer and Klipper questions** — kinematics, slicers,
  probes, TMC drivers, tuning, and everything Helix shares unchanged with
  upstream Klipper — are well served by the large **upstream Klipper
  community** below. Because Helix is source-compatible with Klipper, that
  knowledge applies directly.

When in doubt: if the behavior only exists because you turned on a Helix
feature, use the Helix GitHub; otherwise the Klipper community is your
fastest answer.

## The upstream Klipper community

These are run by the upstream Klipper project, not by Helix. They are the
right venue for general 3D-printer-firmware questions.

### Discourse Forum

There is a
[Klipper Community Discourse server](https://community.klipper3d.org)
for "forum" style discussions. Note that Discourse is not Discord.

### Discord Chat

There is a Discord server dedicated to Klipper at
[discord.klipper3d.org](https://discord.klipper3d.org). Note that Discord
is not Discourse. It is run by a community of Klipper enthusiasts and lets
you chat with other users in real time.

## I have a question

First, check the documentation — many questions are already answered.
Start with the [Helix documentation overview](Overview.md), and if you are
coming from Klipper, [Coming from Klipper](Coming_From_Klipper.md).

- **Helix-specific question:** open a
  [GitHub Discussion](https://github.com/jrlomas/klipper/discussions) on
  the Helix project.
- **General printing or Klipper question:** search or post in the
  [Klipper Discourse Forum](#discourse-forum) or
  [Klipper Discord Chat](#discord-chat), or a forum dedicated to your
  printer hardware.

If you are experiencing a general printing problem, first carefully
inspect the printer hardware (joints, wires, screws) — most printing
problems are mechanical, not firmware.

## I have a feature request

Every feature needs someone willing to build **and maintain** it — that is
a core part of how Helix works (see
[Our philosophy](CONTRIBUTING.md#our-philosophy-pragmatic-and-you-maintain-what-you-add)).
Open a [GitHub Discussion](https://github.com/jrlomas/klipper/discussions)
or [issue](https://github.com/jrlomas/klipper/issues) on the Helix project
to propose one or to offer to help implement or test existing work.

## I found a bug

Helix is open-source and we appreciate careful bug reports. **Report Helix
bugs on the Helix GitHub:**
[github.com/jrlomas/klipper/issues](https://github.com/jrlomas/klipper/issues).

Before reporting, gather the information that makes a bug fixable:

1. **Confirm it's a Helix issue.** If the problem also happens on a
   configuration that uses none of Helix's features, it may be a general
   Klipper issue — the [Klipper community](#the-upstream-klipper-community)
   can help there. If it only appears with a Helix feature enabled, it
   belongs on the Helix GitHub. (Do **not** try to reproduce a
   Helix-specific failure on upstream Klipper — the Helix motion path
   doesn't exist there.)
2. **Capture the shutdown state.** If possible, run an `M112` command
   immediately after the undesirable event. This puts the firmware into a
   shutdown state and writes extra debugging information to the log.
3. **Attach the full log file.** The log is `klippy.log`, engineered to
   answer the common questions a maintainer will have (software version,
   hardware type, configuration, event timing, and much more).
   - A Helix/Klipper web interface (Mainsail, Fluidd) can download the log
     directly — the easiest option. Otherwise copy it with `scp`/`sftp`
     (WinSCP on Windows). It is usually at
     `~/printer_data/logs/klippy.log`, and sometimes at `/tmp/klippy.log`.
   - Attach the **full, unmodified** log — not a snippet. Only the
     complete log has the necessary context. Compressing it with zip or
     gzip is appreciated.
4. **Open an issue** on the
   [Helix GitHub](https://github.com/jrlomas/klipper/issues) with a clear
   description: what you did, what you expected, and what actually
   happened. Attach the compressed log.

## I'd like to contribute a change

Helix welcomes contributions. Read the
[Contributing guide](CONTRIBUTING.md) first — especially
[How to change Helix without fighting upstream](CONTRIBUTING.md#how-to-change-helix-without-fighting-upstream),
which explains where a change should live so it stays merge-clean, and the
expectation that you maintain what you add. The
[developer documentation](Overview.md#developer-documentation) and the
[Helix Developer Guide](Helix_Developer_Guide.md) cover the architecture.
Open a [pull request or discussion](https://github.com/jrlomas/klipper) on
the Helix project when you're ready.
