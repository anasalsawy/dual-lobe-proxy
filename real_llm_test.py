#!/usr/bin/env python3
"""
Real LLM-based end-to-end testing for dual-lobe-proxy.

This script makes actual API calls to the running proxy server
to validate routing behavior with real LLM intelligence.
"""

import sys
import asyncio
import json
import time
from typing import List, Dict, Any
import requests

# Test configuration
PROXY_URL = "http://localhost:8801/v1"
API_KEY = "dl_357a343ae5107707f9a6b708f0c5f39592519ef282e21d24"
MODEL_ID = "sawii/dual-lobe"
MESSAGE_TIMEOUT = 30  # seconds

class RealLLMTestRunner:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        })
        self.test_results = []
        
    async def make_api_call(self, messages: List[Dict[str, str]], 
                           model: str = MODEL_ID,
                           max_tokens: int = 100,
                           temperature: float = 0.0) -> Dict[str, Any]:
        """Make a real API call to the dual-lobe proxy."""
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False
        }
        
        try:
            response = self.session.post(
                f"{PROXY_URL}/chat/completions",
                json=payload,
                timeout=MESSAGE_TIMEOUT
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ API call failed: {e}")
            return {"error": str(e)}
    
    def log_test_result(self, test_name: str, success: bool, details: str = ""):
        """Log test result."""
        result = {
            "test_name": test_name,
            "success": success,
            "details": details,
            "timestamp": time.time()
        }
        self.test_results.append(result)
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"   {status} {test_name}")
        if details:
            print(f"      Details: {details}")
    
    async def test_basic_responsiveness(self):
        """Test 1: Basic responsiveness - ensure the proxy responds."""
        print("\n🧪 Test 1: Basic responsiveness")
        response = await self.make_api_call([
            {"role": "user", "content": "Hello, are you working?"}
        ])
        
        if "error" in response:
            self.log_test_result("Basic responsiveness", False, f"API error: {response['error']}")
            return False
        
        if "choices" not in response or len(response["choices"]) == 0:
            self.log_test_result("Basic responsiveness", False, "No choices in response")
            return False
            
        content = response["choices"][0]["message"]["content"]
        if len(content) > 0:
            self.log_test_result("Basic responsiveness", True, f"Response length: {len(content)} chars")
            return True
        else:
            self.log_test_result("Basic responsiveness", False, "Empty response")
            return False
    
    async def test_model_listing(self):
        """Test 2: Model listing endpoint."""
        print("\n🧪 Test 2: Model listing")
        try:
            response = self.session.get(f"{PROXY_URL}/models", timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "data" in data and len(data["data"]) > 0:
                model_ids = [model["id"] for model in data["data"]]
                expected_models = ["sawii/dual-lobe", "sawii/dl-dialogue"]
                found_models = [m for m in expected_models if m in model_ids]
                
                if len(found_models) > 0:
                    self.log_test_result("Model listing", True, f"Found models: {found_models}")
                    return True
                else:
                    self.log_test_result("Model listing", False, f"Expected models not found. Available: {model_ids}")
                    return False
            else:
                self.log_test_result("Model listing", False, "No models in response")
                return False
                
        except Exception as e:
            self.log_test_result("Model listing", False, f"Request failed: {e}")
            return False
    
    async def test_conversation_flow(self):
        """Test 3: Real conversation flow with multiple turns."""
        print("\n🤖 Test 3: Conversation flow")
        messages = [
            {"role": "user", "content": "What is your name and role?"},
        ]
        
        response1 = await self.make_api_call(messages)
        if "error" in response1:
            self.log_test_result("Conversation flow", False, f"First message failed: {response1['error']}")
            return False
            
        assistant_response1 = response1["choices"][0]["message"]["content"]
        messages.extend([
            response1["choices"][0]["message"],
            {"role": "user", "content": "Great! Now tell me about the dual-lobe architecture."}
        ])
        
        response2 = await self.make_api_call(messages)
        if "error" in response2:
            self.log_test_result("Conversation flow", False, f"Second message failed: {response2['error']}")
            return False
            
        assistant_response2 = response2["choices"][0]["message"]["content"]
        
        if len(assistant_response1) > 0 and len(assistant_response2) > 0:
            self.log_test_result("Conversation flow", True, 
                               f"Turn 1: {len(assistant_response1)} chars, Turn 2: {len(assistant_response2)} chars")
            return True
        else:
            self.log_test_result("Conversation flow", False, "Empty responses")
            return False
    
    async def test_error_handling(self):
        """Test 4: Error handling with invalid requests."""
        print("\n🧪 Test 4: Error handling")
        try:
            # Test with invalid model
            response = await self.make_api_call(
                [{"role": "user", "content": "test"}],
                model="nonexistent-model"
            )
            
            # Should get an error response
            if "error" in response or (isinstance(response, dict) and "error" in str(response).lower()):
                self.log_test_result("Error handling", True, "Proper error response for invalid model")
                return True
            else:
                self.log_test_result("Error handling", False, "No error for invalid model")
                return False
                
        except Exception as e:
            # If we get an exception, that's also acceptable for error handling
            self.log_test_result("Error handling", True, f"Exception properly handled: {e}")
            return True
    
    async def run_all_tests(self):
        """Run all real LLM tests."""
        print("=" * 80)
        print("🤖 REAL LLM END-TO-END TESTING")
        print("🚀 Testing against live dual-lobe-proxy server")
        print("=" * 80)
        
        # Test basic connectivity first
        if not await self.test_basic_responsiveness():
            print("\n❌ Basic connectivity failed. Aborting further tests.")
            return False
        
        # Run all tests
        test_functions = [
            self.test_model_listing,
            self.test_conversation_flow,
            self.test_error_handling
        ]
        
        passed = 0
        total = len(test_functions) + 1  # +1 for basic responsiveness
        
        for test_func in test_functions:
            if await test_func():
                passed += 1
        
        print("\n" + "=" * 80)
        print(f"📊 REAL LLM TEST RESULTS: {passed}/{total} tests passed")
        
        if passed == total:
            print("🎉 ALL REAL LLM TESTS PASSED!")
            print("✅ The dual-lobe-proxy is working correctly with real LLM calls!")
            return True
        else:
            print(f"⚠️  {total - passed} tests failed. Investigation needed.")
            return False

async def main():
    """Main entry point."""
    runner = RealLLMTestRunner()
    success = await runner.run_all_tests()
    return success

if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)