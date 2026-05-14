#!/usr/bin/env python3
"""
Dynamic MCP Proxy Server with Guardrails
Automatically discovers tools from MongoDB MCP Server and applies security filters.

When MongoDB updates their MCP server, this proxy automatically:
1. Discovers new tools via list_tools()
2. Blocks dangerous tools via BLOCKED_TOOLS
3. Applies guardrails to allowed operations
"""

import sys
import os
import json
import logging
import re
from typing import Any, Dict, List, Set
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging to stderr (stdout is for MCP protocol)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

# Import MCP client
from mcp_client import get_mcp_client, MCPStdioClient


# ===========================================
# SECURITY CONFIGURATION
# ===========================================

class SecurityConfig:
    """
    Security configuration for the proxy.
    Edit this class to adjust what's allowed.
    """
    
    # Tools that are COMPLETELY BLOCKED (never exposed to VS Code)
    BLOCKED_TOOLS: Set[str] = {
        # Write operations
        "insert-many",
        "insertMany", 
        "update-many",
        "updateMany",
        "delete-many",
        "deleteMany",
        
        # Destructive operations
        "drop-collection",
        "dropCollection",
        "drop-database",
        "dropDatabase",
        "drop-index",
        "dropIndex",
        
        # Schema modifications
        "create-collection",
        "createCollection",
        "create-index",
        "createIndex",
        "rename-collection",
        "renameCollection",
        
        # Atlas management (if exposed)
        "atlas-local-create-deployment",
        "atlas-local-delete-deployment",
    }
    
    # Tools that need parameter validation
    TOOLS_WITH_VALIDATION: Set[str] = {
        "find",
        "aggregate", 
        "count",
        "collection-schema",
        "collection-indexes",
        "list-collections",
    }
    
    # Protected databases
    PROTECTED_DATABASES: Set[str] = {
        "admin",
        "local", 
        "config",
    }
    
    # Dangerous aggregation stages
    BLOCKED_AGG_STAGES: Set[str] = {
        "$out",
        "$merge",
    }
    
    # Dangerous operators in filters
    BLOCKED_OPERATORS: Set[str] = {
        "$where",
        "$function",
        "$accumulator",
    }
    
    # Limits
    MAX_LIMIT = 100
    MAX_PIPELINE_STAGES = 10


class SecurityViolation(Exception):
    """Raised when a guardrail is triggered."""
    pass


class GuardrailsValidator:
    """Validates operations against security rules."""
    
    @classmethod
    def validate_database(cls, database: str) -> None:
        """Check if database access is allowed."""
        if database.lower() in {db.lower() for db in SecurityConfig.PROTECTED_DATABASES}:
            raise SecurityViolation(
                f"🚫 Acceso denegado: Base de datos '{database}' está protegida."
            )
    
    @classmethod
    def validate_filter(cls, filter_dict: Dict) -> None:
        """Check filter for dangerous operators."""
        if not filter_dict:
            return
        
        filter_str = json.dumps(filter_dict).lower()
        for op in SecurityConfig.BLOCKED_OPERATORS:
            if op.lower() in filter_str:
                raise SecurityViolation(
                    f"🚫 Operador bloqueado: '{op}' no está permitido."
                )
    
    @classmethod
    def validate_pipeline(cls, pipeline: List[Dict]) -> None:
        """Check aggregation pipeline for dangerous stages."""
        if not pipeline:
            return
        
        if len(pipeline) > SecurityConfig.MAX_PIPELINE_STAGES:
            raise SecurityViolation(
                f"🚫 Pipeline muy largo: máximo {SecurityConfig.MAX_PIPELINE_STAGES} etapas."
            )
        
        for stage in pipeline:
            stage_str = json.dumps(stage).lower()
            for blocked in SecurityConfig.BLOCKED_AGG_STAGES:
                if blocked.lower() in stage_str:
                    raise SecurityViolation(
                        f"🚫 Stage bloqueado: '{blocked}' no está permitido."
                    )
    
    @classmethod
    def sanitize_arguments(cls, tool_name: str, arguments: Dict) -> Dict:
        """Sanitize and validate arguments for a tool."""
        sanitized = arguments.copy()
        
        # Validate database access
        if "database" in sanitized:
            cls.validate_database(sanitized["database"])
        
        # Validate filter
        if "filter" in sanitized:
            cls.validate_filter(sanitized.get("filter", {}))
        
        # Validate and limit results
        if "limit" in sanitized:
            sanitized["limit"] = min(
                sanitized.get("limit", 10), 
                SecurityConfig.MAX_LIMIT
            )
        
        # Validate pipeline
        if "pipeline" in sanitized:
            cls.validate_pipeline(sanitized.get("pipeline", []))
        
        return sanitized


# ===========================================
# DYNAMIC TOOL DISCOVERY
# ===========================================

class DynamicToolRegistry:
    """
    Discovers and filters tools from the upstream MCP server.
    Automatically adapts when MongoDB updates their tools.
    """
    
    def __init__(self):
        self._tools_cache: List[Dict] = []
        self._tools_loaded = False
        self._client: MCPStdioClient = None
    
    def _get_client(self) -> MCPStdioClient:
        """Get or create MCP client."""
        if self._client is None:
            self._client = get_mcp_client()
        return self._client
    
    def discover_tools(self) -> List[Dict]:
        """
        Discover tools from upstream MongoDB MCP server.
        Filters out blocked tools automatically.
        """
        if self._tools_loaded:
            return self._tools_cache
        
        try:
            client = self._get_client()
            all_tools = client.list_tools()
            
            logger.info(f"Discovered {len(all_tools)} tools from MongoDB MCP")
            
            # Filter blocked tools
            allowed_tools = []
            blocked_count = 0
            
            for tool in all_tools:
                tool_name = tool.get("name", "")
                
                if tool_name in SecurityConfig.BLOCKED_TOOLS:
                    logger.info(f"🚫 Blocking tool: {tool_name}")
                    blocked_count += 1
                else:
                    # Add guardrail notice to description
                    tool_copy = tool.copy()
                    if tool_name in SecurityConfig.TOOLS_WITH_VALIDATION:
                        original_desc = tool_copy.get("description", "")
                        tool_copy["description"] = f"🛡️ [GUARDRAILS] {original_desc}"
                    allowed_tools.append(tool_copy)
            
            logger.info(f"Exposing {len(allowed_tools)} tools, blocked {blocked_count}")
            
            self._tools_cache = allowed_tools
            self._tools_loaded = True
            
            return allowed_tools
            
        except Exception as e:
            logger.error(f"Error discovering tools: {e}")
            return []
    
    def call_tool(self, name: str, arguments: Dict) -> Dict:
        """
        Call a tool on the upstream server with guardrails.
        """
        # Check if tool is blocked
        if name in SecurityConfig.BLOCKED_TOOLS:
            return {
                "content": [{
                    "type": "text",
                    "text": f"🚫 Tool '{name}' está bloqueado por políticas de seguridad."
                }]
            }
        
        try:
            # Apply guardrails
            sanitized_args = GuardrailsValidator.sanitize_arguments(name, arguments)
            
            # Call upstream
            client = self._get_client()
            result = client.call_tool(name, sanitized_args)
            
            return result
            
        except SecurityViolation as e:
            return {
                "content": [{
                    "type": "text",
                    "text": f"🛡️ {str(e)}"
                }]
            }
        except Exception as e:
            logger.error(f"Tool call error: {e}")
            return {
                "error": str(e)
            }


# ===========================================
# MCP PROXY SERVER
# ===========================================

class MCPProxyServer:
    """
    MCP Proxy Server that:
    1. Dynamically discovers tools from MongoDB MCP
    2. Filters blocked tools
    3. Applies guardrails to requests
    """
    
    def __init__(self):
        self.initialized = False
        self.registry = DynamicToolRegistry()
    
    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle incoming MCP request."""
        method = request.get("method", "")
        request_id = request.get("id")
        params = request.get("params", {})
        
        logger.info(f"Received: {method}")
        
        try:
            if method == "initialize":
                return self._handle_initialize(request_id, params)
            elif method == "initialized":
                return None
            elif method == "tools/list":
                return self._handle_tools_list(request_id)
            elif method == "tools/call":
                return self._handle_tools_call(request_id, params)
            elif method == "ping":
                return self._success_response(request_id, {})
            else:
                return self._error_response(request_id, -32601, f"Method not found: {method}")
        except Exception as e:
            logger.error(f"Error: {e}")
            return self._error_response(request_id, -32603, str(e))
    
    def _handle_initialize(self, request_id: int, params: Dict) -> Dict:
        """Initialize the proxy server."""
        self.initialized = True
        
        # Pre-discover tools
        tools = self.registry.discover_tools()
        
        return self._success_response(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "mongodb-secure-proxy",
                "version": "2.0.0",
                "description": f"Dynamic proxy with guardrails. {len(tools)} tools available."
            }
        })
    
    def _handle_tools_list(self, request_id: int) -> Dict:
        """Return filtered tools list."""
        tools = self.registry.discover_tools()
        return self._success_response(request_id, {"tools": tools})
    
    def _handle_tools_call(self, request_id: int, params: Dict) -> Dict:
        """Execute tool with guardrails."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        
        logger.info(f"Calling: {tool_name}")
        
        result = self.registry.call_tool(tool_name, arguments)
        
        # Format response
        if "error" in result:
            return self._success_response(request_id, {
                "content": [{"type": "text", "text": f"Error: {result['error']}"}]
            })
        
        return self._success_response(request_id, result)
    
    def _success_response(self, request_id: int, result: Any) -> Dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    
    def _error_response(self, request_id: int, code: int, message: str) -> Dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    
    def run(self):
        """Run the proxy server (STDIO mode)."""
        logger.info("🛡️ MCP Proxy Server starting...")
        logger.info(f"Blocked tools: {SecurityConfig.BLOCKED_TOOLS}")
        
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            
            try:
                request = json.loads(line)
                response = self.handle_request(request)
                
                if response:
                    print(json.dumps(response), flush=True)
                    
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON: {e}")
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"}
                }), flush=True)


# ===========================================
# MAIN
# ===========================================

if __name__ == "__main__":
    server = MCPProxyServer()
    server.run()
