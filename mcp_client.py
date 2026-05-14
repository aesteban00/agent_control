"""
MCP Client for MongoDB Atlas MCP Server via STDIO
Spawns the MCP Server as a subprocess and communicates via stdin/stdout
"""

import os
import json
import subprocess
import threading
import queue
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPStdioClient:
    """
    Client for communicating with MongoDB MCP Server via STDIO.
    Spawns mongodb-mcp-server as a subprocess.
    """
    
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._response_queue = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._initialized = False
    
    def _start_server(self):
        """Start the MCP server subprocess."""
        if self._process is not None:
            return
        
        mongodb_uri = os.getenv("MONGODB_URI", "")
        
        # Inherit env (incl. MDB_MCP_* loaded from .env by the proxy via load_dotenv).
        # Guardrails (readOnly, indexCheck, maxTimeMS, disabledTools, ...) are
        # configured in .env per https://www.mongodb.com/docs/mcp-server/configuration/options/
        env = os.environ.copy()
        env["MDB_MCP_CONNECTION_STRING"] = mongodb_uri
        
        try:
            self._process = subprocess.Popen(
                ["mongodb-mcp-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1
            )
            
            # Start reader thread
            self._reader_thread = threading.Thread(target=self._read_responses, daemon=True)
            self._reader_thread.start()
            
            logger.info("MongoDB MCP Server started successfully")
            
        except FileNotFoundError:
            logger.error("mongodb-mcp-server not found. Make sure it's installed.")
            raise RuntimeError("mongodb-mcp-server not found")
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            raise
    
    def _read_responses(self):
        """Read responses from the MCP server stdout."""
        if not self._process or not self._process.stdout:
            return
        
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            
            try:
                response = json.loads(line)
                # Only queue actual responses (with id), ignore notifications
                if "id" in response:
                    self._response_queue.put(response)
                elif "method" in response:
                    # It's a notification, log it but don't queue
                    logger.debug(f"MCP notification: {response.get('method')}")
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON response: {line} - {e}")
    
    def _send_request(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a JSON-RPC request to the MCP server."""
        with self._lock:
            if self._process is None:
                self._start_server()
            
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
            }
            if params:
                request["params"] = params
            
            try:
                request_line = json.dumps(request) + "\n"
                self._process.stdin.write(request_line)
                self._process.stdin.flush()
                
                # Wait for response
                try:
                    response = self._response_queue.get(timeout=30)
                    
                    if "error" in response:
                        error = response["error"]
                        return {"error": f"{error.get('message', 'Unknown error')}"}
                    
                    return response.get("result", {})
                    
                except queue.Empty:
                    return {"error": "Timeout waiting for MCP server response"}
                    
            except Exception as e:
                logger.error(f"Error sending request: {e}")
                return {"error": str(e)}
    
    def initialize(self) -> Dict[str, Any]:
        """Initialize the MCP connection."""
        return self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "mcp-secure-agent",
                "version": "1.0.0"
            }
        })
    
    def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools from the MCP server."""
        result = self._send_request("tools/list")
        if "error" in result:
            return []
        return result.get("tools", [])
    
    def call_tool(self, name: str, arguments: Dict[str, Any] = None) -> Dict[str, Any]:
        """Call a tool on the MCP server."""
        return self._send_request("tools/call", {
            "name": name,
            "arguments": arguments or {}
        })
    
    def close(self):
        """Close the MCP server subprocess."""
        if self._process:
            self._process.terminate()
            self._process.wait(timeout=5)
            self._process = None


# ===========================================
# Singleton Instance
# ===========================================

_mcp_client: Optional[MCPStdioClient] = None
_initialized: bool = False


def get_mcp_client() -> MCPStdioClient:
    """Get or create the MCP client singleton."""
    global _mcp_client, _initialized
    
    if _mcp_client is None:
        _mcp_client = MCPStdioClient()
    
    if not _initialized:
        init_result = _mcp_client.initialize()
        if "error" not in init_result:
            _initialized = True
            logger.info("MCP client initialized successfully")
        else:
            logger.warning(f"MCP initialization warning: {init_result.get('error')}")
    
    return _mcp_client


# ===========================================
# Tool Wrapper Functions
# ===========================================

def _extract_text(result: Dict[str, Any]) -> str:
    """Extract text content from MCP tool result."""
    if "error" in result:
        return f"Error: {result['error']}"
    
    content = result.get("content", [])
    if content and isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(item.get("text", ""))
        return "\n".join(texts) if texts else json.dumps(result, indent=2)
    
    return json.dumps(result, indent=2, default=str)


def mcp_list_databases() -> str:
    """List all MongoDB databases via MCP."""
    client = get_mcp_client()
    result = client.call_tool("listDatabases")
    return _extract_text(result)


def mcp_list_collections(database: str) -> str:
    """List collections in a database via MCP."""
    client = get_mcp_client()
    result = client.call_tool("listCollections", {"database": database})
    return _extract_text(result)


def mcp_find(database: str, collection: str, filter_dict: Dict = None, limit: int = 10) -> str:
    """Execute a find query via MCP."""
    client = get_mcp_client()
    args = {
        "database": database,
        "collection": collection,
        "limit": limit
    }
    if filter_dict:
        args["filter"] = filter_dict
    
    result = client.call_tool("find", args)
    return _extract_text(result)


def mcp_aggregate(database: str, collection: str, pipeline: List[Dict]) -> str:
    """Execute an aggregation pipeline via MCP."""
    client = get_mcp_client()
    result = client.call_tool("aggregate", {
        "database": database,
        "collection": collection,
        "pipeline": pipeline
    })
    return _extract_text(result)


def mcp_collection_schema(database: str, collection: str) -> str:
    """Get collection schema via MCP."""
    client = get_mcp_client()
    result = client.call_tool("collectionSchema", {
        "database": database,
        "collection": collection
    })
    return _extract_text(result)


def mcp_collection_indexes(database: str, collection: str) -> str:
    """Get collection indexes via MCP."""
    client = get_mcp_client()
    result = client.call_tool("collectionIndexes", {
        "database": database,
        "collection": collection
    })
    return _extract_text(result)


def mcp_count(database: str, collection: str, filter_dict: Dict = None) -> str:
    """Count documents in a collection via MCP."""
    client = get_mcp_client()
    args = {
        "database": database,
        "collection": collection
    }
    if filter_dict:
        args["query"] = filter_dict
    
    result = client.call_tool("count", args)
    return _extract_text(result)


def mcp_update_many(database: str, collection: str, filter_dict: Dict, update_dict: Dict) -> str:
    """Execute an updateMany operation via MCP."""
    import json
    client = get_mcp_client()
    logger.info(f"Calling update-many with: database={database}, collection={collection}, filter={filter_dict}, update={update_dict}")
    result = client.call_tool("update-many", {
        "database": database,
        "collection": collection,
        "filter": filter_dict,
        "update": update_dict
    })
    logger.info(f"update-many raw result: {json.dumps(result) if result else 'None'}")
    return _extract_text(result)


def mcp_list_tools() -> str:
    """List all available tools from the MCP server."""
    client = get_mcp_client()
    tools = client.list_tools()
    tool_names = [t.get("name", "unknown") for t in tools]
    logger.info(f"Available MCP tools: {tool_names}")
    return str(tool_names)
