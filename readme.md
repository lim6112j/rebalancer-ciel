# how to run
            source ./venv/bin/activate
            python rl_rebalancer.py -m train && python plot_rl_rewards.py -m train
            python rl_rebalancer.py -m test && python plot_rl_rewards.py -m test
