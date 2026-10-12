"""jev-kit: one command for installing, configuring, updating and inspecting the kit.

    jev-kit status | config | rollback | metrics | recall | pricing | headless

Every command that writes prints what it would change and needs --yes. The
package reads airlock's own path and rule modules, so it never guesses where
config lives.
"""
