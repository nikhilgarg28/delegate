Standup is an agentic system to run a team composed of some agents and some 
humans under the supervision of a manager.

All team members (agents or humans) have access to two common substraits (both
implemented via regular files):

1. A task/project management system
2. A system of peer to peer message queues

The team has a few stakeholders:
1. Director - the human external to the system who directs/controls manager. 
   Manager acts on the behalf of the Director. Team members don't know about the
   Director - they only talk to each other and the manager.
2. Manager - the agent (with own .md file) which writes the team charter (as 
    instructed by the director), interprets and enforces it. It typically will
    talk to the director to understand & clarify project specs, break it down in
    tasks of appropriate complexity, update internal task/management system,
    assign tasks to appropriate teammates based on complexity, expertise, and
    background, chooses the order in which tasks are to be done, and acts as
    first line of defense to take/resolve questions from teammates.
1. One or more team mates - some human and some agents (for now, only agent).
    Each agent is basically a claude agent for now.


Team Charter
=============
Charter is a document (md file) describng how the team should operate - it contains
information about the cultural values, processes, operating procedures etc. It
is written by the manager based on instructs from the director - manager can
also propose changes to it and present them to Director for approval.

Roster
======
At any point of time, team has some members a file called roster.md enumerates
the names of all the active members along with a short description of what they
own and their roles in the team.

Tasks
=====
There is a python file in scripts/task.py which contains machinery to manage 
tasks & projects. Tasks themselves are stored as files.

Chat
====
All messages sent to chat are also stored in sqlite and managed via a python
script in scripts/chat.py

Mailboxes
=========
Every teammate gets an outbox and an inbox. When it wants to send message to 
another teammate, it writes a message to own outbox. Messages that others are
sending it come to its inbox. This is managed via scripts/mailbox.py

Only agent writes to its outbox, only daemon reads from outbox.
Only daemon writes to any inbox, only the owner agent reads it.

Outbox message has format of (send_time, recipient, message)
Inbox message has format of (send_time, sender, message)

Daemon
======
Daemon is an event loop (non-LLM) which listens to outboxes of all 
teammates and dispatches them to appropriate inbox of the recipient. Daemon
also runs a web app server for the human director to interact with the team. 
This app shows a view of the projects/tasks as well as in a separate tab, a chat
box. All messages sent from one member to another show up here for the director
to observe. In addition, all events (e.g. assigning of tasks, task status 
changes, spawning o agents etc) also show up here.

More importantly, director can send messages to the manager through the chat box
who can also respond back. If the manager has a question for the director (say
because a teammate asked them something and they didn't know the answer), they
can also send a message to director's mailbox.

Agent's Memory
==============
All agents maintain a folder called `memory` with a few kinds of files:
1. Journals - here they can write one or more <number>.journal.md files 
where they periodically write down relevant memories - understanding, decisions, 
open questions, lessons, goals etc. 

2. Notes - here they contain many files about variety of topics of their choice
as {name}.note.md. 

3. Feedback - one file for every other team mate they have worked with, containing
feedback for them based on the quality of their work

4. Context - a single context.md file which contains a summary of their current
short session state - when the agent comes back up the next time, they minimally
read this file to get going.

Repos
=====
A team has one or more shared git repos (including a special repo called `meta`)
which contains standup related files to govern the team. Agents, when working
on a repo, clone the repo in their own directory, work in a branch, and raise
the PR against the main repo.

CI
==
Every team has a CI agent with own mailboxes. Whenever an agent wants to raise
a PR against the main repo, they send a message to CI. CI agent can choose to run
tests

Directory Structure
===================
root
    - workspace
        - <agent1>
            - <repo1>  # cloned copy of repos this agent is working on
            - <repo2>
        - <agent2>
    - repos
        - <repo1>
            .git
            ...
        - <repo2>
            .git
            ...
        - <meta>
            - .git
            - charter.md
            - scripts
                - <script1>
                - <script2>
                - run.py # special script that starts daemon
            - team
                - roster.md
                - <agent1>
                    - bio.md
                    - state.yaml
                    - outbox/
                        out_1.csv # outbox, one is active, older are archived
                        out_2.csv
                    - inbox/
                        in_1.csv
                        in_2.csv
                    - journals
                        - 1.journal.md
                        - 2.journal.md
                    - notes
                        - <topic1>.note.md
                        - <topic2>.note.md
                    - feedback
                        - <teammate1>.md
                        - <teammate2>.md
                    - context.md
                    - logs/
                        - 1.worklog.md  # contains full log of all prompts of this agent
                        - 2.worklog.md
                - <agent2>
        - db.sqlite


State file
==========
Every agent gets a state file. This stores metadata like:
- PID: the pid of the process executing this agent right now (or None)
- in_cursor: id of the message in inbox until which processed (we try to do at least
once processing so id is incremented after finishing the action)
- out_cursor: next id of the message to be sent in outbox
