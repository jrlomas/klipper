#ifndef __TRAJ_LOCAL_H
#define __TRAJ_LOCAL_H

#include "autoconf.h" // CONFIG_WANT_TRAJECTORY

#if CONFIG_WANT_TRAJECTORY
void traj_local_hold_all(void);
#else
static inline void traj_local_hold_all(void) { }
#endif

#endif // traj_local.h
