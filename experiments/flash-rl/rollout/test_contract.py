import copy
import json
from pathlib import Path
import unittest
from run import validate_response, unfiltered_request

class TokenContract(unittest.TestCase):
    def setUp(self):
        self.reply=json.loads((Path(__file__).parent/'capability-probe.json').read_text())['response']
    def test_actual_reasoning_tokens_fully_covered(self):
        value=validate_response(self.reply)
        self.assertEqual(len(value['old_logprobs']),8)
        self.assertEqual(self.reply['usage']['completion_tokens_details']['reasoning_tokens'],8)
    def test_missing_prompt_ids_rejected(self):
        self.reply.pop('prompt_token_ids')
        with self.assertRaises(ValueError):validate_response(self.reply)
    def test_missing_reasoning_logprob_rejected(self):
        self.reply['choices'][0]['logprobs']['content'].pop()
        with self.assertRaises(ValueError):validate_response(self.reply)
    def test_misaligned_ids_rejected(self):
        self.reply['choices'][0]['token_ids'][0]+=1
        with self.assertRaises(ValueError):validate_response(self.reply)
    def test_nan_rejected(self):
        self.reply['choices'][0]['logprobs']['content'][0]['logprob']=float('nan')
        with self.assertRaises(ValueError):validate_response(self.reply)
class TrainingSampling(unittest.TestCase):
    def test_forced_submit_keeps_instruction_and_tools_but_samples_auto(self):
        body={'tool_choice':{'type':'function','function':{'name':'submit_rca'}},
              'messages':[{'role':'user','content':'조사 예산이 끝났다. submit_rca 를 호출하라.'}],
              'tools':[{'type':'function','function':{'name':'submit_rca'}}]}
        before=copy.deepcopy(body)
        wire=unfiltered_request(body)
        self.assertEqual(wire['tool_choice'],'auto')
        self.assertEqual(wire['messages'],before['messages'])
        self.assertEqual(wire['tools'],before['tools'])
        self.assertEqual(body,before)
        self.assertEqual((wire['temperature'],wire['top_p'],wire['top_k'],wire['min_p']),(1,1,-1,0))
    def test_other_grammar_not_silently_removed(self):
        with self.assertRaises(ValueError):unfiltered_request({'tool_choice':'auto','guided_json':{}})
    def test_unknown_forced_tool_not_silently_removed(self):
        with self.assertRaises(ValueError):unfiltered_request({'tool_choice':{'type':'function','function':{'name':'delete_db'}}})
if __name__=='__main__':unittest.main()
