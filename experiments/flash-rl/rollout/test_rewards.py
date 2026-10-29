import unittest
from finalize import scalar_reward,classify
from rca_lab.train.grpo import EpisodeOutcome,trajectory_rewards

class RewardParity(unittest.TestCase):
    def test_all_outcomes_match_authoritative_core(self):
        for p,r,m,fail in [(1,1,1,False),(1,1,.5,False),(.5,1,1,False),(1,.5,.5,False),(0,0,0,False),(0,0,0,True)]:
            for weight in (0,.2):
                for n,t in [(100,1),(10000,45)]:
                    success=p==r==m==1
                    outcome=EpisodeOutcome('completed',p==r==1,m==1,n,t,root_precision=p,root_recall=r,model_failure=fail,mechanism_score=m)
                    expected=trajectory_rewards([outcome],token_budget=1000,turn_budget=10,efficiency_weight=weight).item()
                    actual=scalar_reward(root_precision=p,root_recall=r,mechanism_score=m,success=success,model_failure=fail,generated_tokens=n,investigation_turns=t,token_reference=1000,turn_reference=10,efficiency_weight=weight)
                    self.assertAlmostEqual(actual,expected,places=6)
    def test_unknown_infrastructure_never_penalized(self):
        for score in [{'stop_reason':'http_error'},{'stop_reason':'submitted','unscored':True},{'stop_reason':'max_turns','infrastructure_error':True}]:
            with self.assertRaises(ValueError):classify(score)
    def test_known_model_failures(self):
        self.assertTrue(classify({'stop_reason':'no_tool_call'}))
        self.assertFalse(classify({'stop_reason':'submitted'}))
if __name__=='__main__':unittest.main()
