import itertools
import numpy as np
from functools import partial
from tot.models import gpt
from tot.tasks import wikienv, wrappers
import requests
import logging

# Configuring the logging
logging.basicConfig(filename='tot_it.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', filemode='w')

# Test logging
logging.info("This is a test log entryyyyyy.")

env = wikienv.WikiEnv()
env = wrappers.HotPotQAWrapper(env, split="train")
env = wrappers.LoggingWrapper(env)

logging.info("Logging has been configured.")

global reflection_map
reflection_map = []

def step(env, action):
    attempts = 0
    while attempts < 10:
        try:
            return env.step(action)
        except requests.exceptions.Timeout:
            attempts += 1

def get_value(task, x, y, n_evaluate_sample, cache_value=True):
    global reflection_map
    unique_trajectories = get_unique_trajectories(failed_trajectories)
    value_prompt = task.value_prompt_wrap(x, y, unique_trajectories, reflection_map)
    logging.info(f"Current: {x}")
    logging.info(f"Current: {y}")
    if cache_value and value_prompt in task.value_cache:
        return task.value_cache[value_prompt]
    logging.info(f"VALUE PROMPT: {value_prompt}")
    value_outputs = gpt(value_prompt, n=n_evaluate_sample, stop=None)
    logging.info(f"VALUE OUTPUTS: {value_outputs}")
    value = task.value_outputs_unwrap(value_outputs)
    logging.info(f"VALUES: {value}")
    if cache_value:
        task.value_cache[value_prompt] = value
    return value

def get_values(task, x, ys, n_evaluate_sample, cache_value=True):
    values = []
    local_value_cache = {}
    for y in ys:  # each partial output
        if y in local_value_cache:  # avoid duplicate candidates
            value = 0
        else:    
            value = get_value(task, x, y, n_evaluate_sample, cache_value=cache_value)
            local_value_cache[y] = value
        values.append(value)
    return values

def get_samples(task, x, y, n_generate_sample, prompt_sample, stop):
    unique_trajectories = get_unique_trajectories(failed_trajectories)
    global reflection_map
    reflection_map = []
    if prompt_sample == 'standard':
        prompt, reflection_map = task.standard_prompt_wrap(x, y)
    elif prompt_sample == 'cot':
        prompt, reflection_map = task.cot_prompt_wrap(x, y, unique_trajectories)
    else:
        raise ValueError(f'prompt_sample {prompt_sample} not recognized')
    logging.info(f"PROMPT: {prompt}")
    samples = gpt(prompt, n=n_generate_sample, stop=stop)
    return [y + _ for _ in samples]

def get_unique_trajectories(failed_trajectories, num=2):
    unique_trajectories = []
    seen_final_answers = set()
    for traj in failed_trajectories:
        final_answer = traj.get('final_answer')
        if final_answer not in seen_final_answers:
            unique_trajectories.append(node_trajectory_to_text(traj['trajectory']))
            seen_final_answers.add(final_answer)
        if len(unique_trajectories) >= num:
            break
    return unique_trajectories

class Node:
    def __init__(self, state, question, parent=None):
        self.state = {'thought': '', 'action': '', 'observation': ''} if state is None else state
        self.parent = parent
        self.question = question
        self.children = []
        self.visits = 0
        self.value = 0
        self.depth = 0 if parent is None else parent.depth + 1
        self.is_terminal = False
        self.reward = 0
        self.exhausted = False # If all children are terminal
        self.em = 0  # Exact match, evaluation metric

    def uct(self):
        if self.visits == 0:
            #return float('inf')
            return self.value * 2
        return self.value / self.visits + np.sqrt(2 * np.log(self.parent.visits) / self.visits)
    
    def uct_with_depth(self, C1=1, C2=1):
        if self.visits == 0:
            return self.value
        exploitation_term = self.value / self.visits
        exploration_term = np.sqrt(2 * np.log(self.parent.visits) / self.visits)
        depth_term = self.depth
        return exploitation_term + C1 * exploration_term + C2 * depth_term

    def __str__(self):
        return f"Node(depth={self.depth}, value={self.value:.2f}, visits={self.visits}, thought={self.state['thought']}, action={self.state['action']}, observation={self.state['observation']})"
    
    def to_dict(self):
        return {
            'state': self.state,
            'question': self.question,
            'parent': self.parent.to_dict() if self.parent else None,
            'children': [child.to_dict() for child in self.children],
            'visits': self.visits,
            'value': self.value,
            'depth': self.depth,
            'is_terminal': self.is_terminal,
            'reward': self.reward,
            'em': self.em,
        }
    
def node_trajectory_to_text(node_string):
    lines = node_string.split('\n')
    formatted_lines = []
    for line in lines:
        try:
            depth = int(line.split(",")[0].split("=")[1].strip())
            thought = line.split(", thought=")[1].split(", action=")[0].strip()
            action = line.split(", action=")[1].split(", observation=")[0].strip()
            observation = line.split(", observation=")[1].split(")")[0].strip()
        except IndexError:
            continue
        
        if depth != 0:
            if thought:
                formatted_lines.append(f"Thought {depth}: {thought}")
            if action:
                formatted_lines.append(f"Action {depth}: {action}")
            if observation:
                formatted_lines.append(f"Observation {depth}: {observation}")
    
    return '\n'.join(formatted_lines)

def collect_all_nodes(node):
        """Recursively collect all nodes starting from the given node."""
        nodes = [node]
        for child in node.children:
            nodes.extend(collect_all_nodes(child))
        return nodes

def collect_trajectory(node):
    trajectory = []
    while node:
        trajectory.append(str(node))
        node = node.parent
    return '\n'.join(reversed(trajectory))

def dfs_search(args, task, idx, depth_limit=7, to_print=True):
    global gpt
    global failed_trajectories
    gpt = partial(gpt, model=args.backend, temperature=args.temperature)
    x = env.reset(idx=idx)
    if to_print:
        print(idx, x)
    root = Node(state=None, question=x)
    all_nodes = []
    failed_trajectories = []
    it = 0
    stack = [root]
    while stack and it < 1:
        node = stack.pop()
        logging.info(f"DFS at node depth {node.depth}...")
        
        if node.is_terminal and node.reward == 1:
            logging.info(f"Terminal node with reward 1 found at depth {node.depth}")
            return node.state, node.value, all_nodes, node.reward, node.em

        if node.depth >= depth_limit:
            logging.info("Depth limit reached")
            continue  # go to next iteration

        expand_node(node, args, task)
        stack.extend(reversed(node.children))  # adding all child nodes to stack for DFS

        all_nodes = [(node, node.value) for node in collect_all_nodes(root)]
        logging.info(f"State of all_nodes after iteration: {all_nodes}")
        it += 1
    # If we reach here, no solution was found
    logging.info("All paths explored. No solution found.")
    return root, 0, all_nodes, 0, 0


def select_node_dfs(stack):
    return stack[-1] if stack else None  # return the last node in the stack

def mcts_search(args, task, idx, iterations=50, to_print=True):
    global gpt
    global failed_trajectories
    gpt = partial(gpt, model=args.backend, temperature=args.temperature)
    x = env.reset(idx=idx)
    if to_print:
        print(idx, x)
    root = Node(state=None, question=x)
    all_nodes = []
    failed_trajectories = []

    for i in range(iterations):
        logging.info(f"Iteration {i + 1}...")
        node = select_node(root)

        while node is None or (node.is_terminal and node.reward != 1):
            logging.info(f"Need to backtrack or terminal node with reward 0 found at iteration {i + 1}, reselecting...")
            node = select_node(root)
        
        if node is None:
            logging.info("All paths lead to terminal nodes with reward 0. Ending search.")
            break

        if node.is_terminal and node.reward == 1:
            logging.info(f"Terminal node with reward 1 found at iteration {i + 1}")
            return node.state, node.value, all_nodes, node.reward, node.em
        
        expand_node(node, args, task)

        while node.is_terminal:
            logging.info(f"Depth limit node found at iteration {i + 1}, reselecting...")
            node = select_node(root)
            expand_node(node, args, task)

        value = evaluate_node(node, args, task)
        backpropagate(node, value)
        all_nodes = [(node, node.value) for node in collect_all_nodes(root)]

        # Check for terminal nodes with a reward of 1
        terminal_nodes_with_reward_1 = [node for node in collect_all_nodes(root) if node.is_terminal and node.reward == 1]
        if terminal_nodes_with_reward_1:
            logging.info(f"Terminal node with reward 1 found at iteration {i + 1}")
            best_node = max(terminal_nodes_with_reward_1, key=lambda x: x.value)
            return best_node.state, best_node.value, all_nodes, best_node.reward, best_node.em
    
        for j, (node, value) in enumerate(all_nodes):
            logging.info(f"Node {j+1}: {str(node)}")

        logging.info(f"State of all_nodes after iteration {i + 1}: {all_nodes}")

    best_child = max(root.children, key=lambda x: x.visits)
    failed_trajectories = []
    if best_child.reward == 1:
        logging.info("Successful trajectory found")
    else:
        logging.info("Unsuccessful trajectory found")
    return best_child.state, best_child.value, all_nodes, best_child.reward, best_child.em

def select_node(node):
    while node and node.children:
        logging.info(f"Selecting from {len(node.children)} children at depth {node.depth}.")
        
        terminal_children = [child for child in node.children if child.is_terminal]
        terminal_status = [child.is_terminal for child in node.children]
        
        if len(terminal_children) == len(node.children):
            logging.info(f"All children are terminal at depth {node.depth}. Backtracking...")
            if node.parent:  
                node.parent.children.remove(node)
            node = node.parent  
            continue  
        
        node_with_reward_1 = next((child for child in terminal_children if child.reward == 1), None)
        if node_with_reward_1:
            logging.info(f"Found terminal node with reward 1 at depth {node.depth}.")
            return node_with_reward_1
        
        node = max((child for child in node.children if not child.is_terminal), key=lambda child: child.uct(), default=None)

        while node.is_terminal and node.reward != 1:
            node = max((child for child in node.parent.children if not child.is_terminal), key=lambda child: child.uct(), default=None)
            
        logging.info(f"Selected node at depth {node.depth} with UCT {node.uct()}.")
        
    return node  # This will return None if all paths from the root are exhausted

def expand_node(node, args, task):
    if node.depth >= 7:
        logging.info("Depth limit reached")
        print("Depth limit reached")
        node.is_terminal = True
        return
    new_nodes = generate_new_states(node, args, task)
    node.children.extend(new_nodes)

def generate_new_states(node, args, task):
    prompt = generate_prompt(node)
    sampled_actions = get_samples(task, prompt, f"Thought {node.depth + 1}: ", args.n_generate_sample, prompt_sample=args.prompt_sample, stop="Observation")
    logging.info(f"SAMPLED ACTION: {sampled_actions}")
    
    unique_states = {}  # Store unique states here
    for action in sampled_actions:
        new_state = node.state.copy()  # Make a copy of the parent node's state

        thought_line = next((line.split(":")[1].strip() for line in action.split("\n") if line.startswith(f"Thought {node.depth + 1}")), '')
        action_line = next((line.split(":")[1].strip() for line in action.split("\n") if line.startswith("Action") and ":" in line), None)


        # Use thought and action to form a unique key
        unique_key = f"{thought_line}::{action_line}"
        
        if unique_key in unique_states:
            continue  # Skip if this state already exists
        
        if action_line:
            action_type = action_line.split('[')[0] if '[' in action_line else action_line
            action_param = action_line.split('[')[1].split(']')[0] if '[' in action_line else ""
            obs, r, done, info = step(env, f"{action_type.lower()}[{action_param}]")

            # Update the new state dictionary
            new_state['thought'] = thought_line
            new_state['action'] = action_line
            new_state['observation'] = obs

            new_node = Node(state=new_state, question=node.question, parent=node)
            new_node.is_terminal = r == 1 or done
            new_node.reward = r
            if r == 1:
                new_node.em = info.get('em')
            unique_states[unique_key] = new_node  # Add this state to unique_states
            logging.info(f"NEW NODE: {new_node}")
            logging.info(f"Feedback: {info}")

            if new_node.is_terminal and r == 0:
                trajectory = collect_trajectory(new_node)
                failed_trajectories.append({'trajectory': trajectory, 'final_answer': f"{action_type.lower()}[{action_param}]"})

    return list(unique_states.values())  # Return unique nodes as a list


def evaluate_node(node, args, task):
    child_prompts = [generate_prompt(child) for child in node.children if not child.is_terminal]
    votes = get_values(task, node.question, child_prompts, args.n_evaluate_sample)
    
    logging.info(f"Length of votes: {len(votes)}")
    logging.info(f"Length of node.children: {len(node.children)}")
    
    # Pre-allocate votes list
    votes = votes + [0] * (len(node.children) - len(votes))
    
    max_vote = max(votes) if votes else 1
    if max_vote == 0:
        max_vote = 1  # Avoid division by zero
    
    terminal_conditions = [1 if child.is_terminal else 0 for child in node.children]
    for i, condition in enumerate(terminal_conditions):
        if condition == 1:
            votes[i] = max_vote + 1
    
    for i, child in enumerate(node.children):
        child.value = votes[i] / max_vote  # Now safe from division by zero
    
    return sum(votes) / len(votes) if votes else 0


def print_tree(node, level=0):
    indent = "  " * level
    print(f"{indent}{node}")
    for child in node.children:
        print_tree(child, level + 1)

def backpropagate(node, value):
    while node:
        node.visits += 1
        if node.is_terminal:
            if node.reward == 1:
                node.value = (node.value * (node.visits - 1) + value) / node.visits
                logging.info(f"Backpropagating with reward 1 at depth {node.depth}. New value: {node.value}.")
            elif node.reward == 0:
                node.value = (node.value * (node.visits - 1) + (-1)) / node.visits
                logging.info(f"Backpropagating with reward 0 at depth {node.depth}. New value: {node.value}.")
        else:
            node.value = (node.value * (node.visits - 1) + value) / node.visits
            logging.info(f"Backpropagating at depth {node.depth}. New value: {node.value}.")

        node = node.parent

def generate_prompt(node):
    trajectory = []
    question = node.question
    while node:
        new_segment = []
        if node.state['thought']:
            new_segment.append(f"Thought {node.depth}: {node.state['thought']}")
        if node.state['action']:
            new_segment.append(f"Action {node.depth}: {node.state['action']}")
        if node.state['observation'] and node.depth != 0:  # Exclude the observation from the root node
            new_segment.append(f"Observation {node.depth}: {node.state['observation']}")
        trajectory.append('\n'.join(new_segment))
        node = node.parent
    return question + '\n'.join(reversed(trajectory))