import multiprocessing
import os
import tempfile
import threading
import time

import pytest

import pandasai as pd
from pandasai.data_loader.duck_db_connection_manager import DuckDBConnectionManager


def worker_process(process_id: int, sample_df: pd.DataFrame):
    try:
        # Each process creates its own manager instance
        manager = DuckDBConnectionManager()

        # Verify each process gets its own db file with correct PID
        assert str(os.getpid()) in manager._db_file

        # Register a table unique to this process
        table_name = f"process_{process_id}_table"
        manager.register(table_name, sample_df)

        # Perform multiple operations
        for i in range(10):
            # Insert some data
            manager.sql(f"INSERT INTO {table_name} VALUES ({i}, 'data_{i}')")
            # Query the data
            result = manager.sql(f"SELECT COUNT(*) FROM {table_name}").df()
            assert result.iloc[0, 0] == i + 1 + 3  # 3 initial rows + i + 1 inserts

        manager.close()
        return True
    except Exception as e:
        return f"Process {process_id} failed: {str(e)}"


class TestDuckDBConnectionManager:
    @pytest.fixture
    def duck_db_manager(self):
        manager = DuckDBConnectionManager()
        yield manager
        manager.close()

    @pytest.fixture
    def sample_df(self):
        return pd.DataFrame({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})

    def test_temp_file_creation_and_deletion(self, duck_db_manager):
        """Test that temporary db file is created and deleted properly"""
        # Get the db file path
        db_file = duck_db_manager._db_file

        # Verify file exists while manager is active
        assert os.path.exists(db_file)

        # Close manager and verify file is deleted
        duck_db_manager.close()
        assert not os.path.exists(db_file)

    def test_connection_pool_exhaustion(self, duck_db_manager):
        """Test that connection requests timeout properly when pool is exhausted"""
        # Get all connections to exhaust the pool
        connections = []
        for _ in range(duck_db_manager._pool_size):
            connections.append(duck_db_manager._get_connection())

        # Test that new request times out after _max_wait_time
        start_time = time.time()
        with pytest.raises(RuntimeError, match="No available connections in the pool"):
            duck_db_manager._get_connection()
        elapsed_time = time.time() - start_time

        # Verify timeout is approximately _max_wait_time
        assert abs(elapsed_time - duck_db_manager._max_wait_time) < 0.5

        # Release connections back to pool
        for conn in connections:
            duck_db_manager._release_connection(conn)

    def test_concurrent_access_thread_safety(self, duck_db_manager, sample_df):
        """Test thread safety with concurrent access"""
        num_threads = 50
        results = []
        errors = []

        def worker():
            try:
                # Register a unique table name per thread
                table_name = f"table_{threading.get_ident()}"
                duck_db_manager.register(table_name, sample_df)

                # Execute a query
                result = duck_db_manager.sql(f"SELECT * FROM {table_name}")
                results.append(result)
            except Exception as e:
                errors.append(str(e))

        # Create and start threads
        threads = []
        for _ in range(num_threads):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        # Wait for all threads to complete
        for t in threads:
            t.join(timeout=10)

        # Verify no errors occurred
        assert not errors
        assert len(results) == num_threads

        # Verify all results are correct
        for result in results:
            assert len(result) == 3  # Should match sample_df row count

    def test_table_registration_thread_safety(self, duck_db_manager, sample_df):
        """Test thread safety of table registration and SQL operations with high concurrency"""
        num_threads = 50
        table_name = "shared_table"
        results = []
        errors = []

        def worker(thread_id):
            try:
                # Register the same table from multiple threads
                duck_db_manager.register(table_name, sample_df)

                # Test various SQL operations
                # 1. Simple select
                select_result = duck_db_manager.sql(f"SELECT * FROM {table_name}")
                assert len(list(select_result.fetchall())) == 3

                # 2. Count query - convert to DataFrame first
                count_result = duck_db_manager.sql(
                    f"SELECT COUNT(*) FROM {table_name}"
                ).df()
                assert count_result.iloc[0, 0] == 3

                # 3. Conditional query
                cond_result = duck_db_manager.sql(
                    f"SELECT col2 FROM {table_name} WHERE col1 = {thread_id % 3 + 1}"
                )
                assert len(list(cond_result.fetchall())) == 1

                # 4. Aggregation - convert to DataFrame first
                agg_result = duck_db_manager.sql(
                    f"SELECT SUM(col1) FROM {table_name}"
                ).df()
                assert agg_result.iloc[0, 0] == 6

                results.append(True)
            except Exception as e:
                errors.append(f"Thread {thread_id} failed: {str(e)}")

        # Create and start threads
        threads = []
        for i in range(num_threads):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        # Wait for all threads to complete
        for t in threads:
            t.join(timeout=10)  # Add timeout to prevent hanging

        # Verify no errors occurred
        assert not errors, f"Errors occurred in threads: {errors}"
        assert len(results) == num_threads, "Not all threads completed successfully"

        # Final verification of table integrity
        final_result = duck_db_manager.sql(f"SELECT * FROM {table_name}")
        assert len(final_result) == 3, "Table data corrupted"

    def test_connection_correct_closing_doesnt_throw(self, duck_db_manager):
        """Test that closing connections doesn't throw exceptions"""
        duck_db_manager.close()

    def test_connection_release_after_multiple_operations(
        self, duck_db_manager, sample_df
    ):
        """Test that connections are properly released after multiple sql and register operations"""
        # Store initial queue size
        initial_queue_size = duck_db_manager._connection_pool.qsize()

        # Define number of iterations
        num_operations = 100

        # Perform multiple register and sql operations
        for i in range(num_operations):
            table_name = f"test_table_{i}"
            # Register new table
            duck_db_manager.register(table_name, sample_df)
            # Execute multiple queries
            duck_db_manager.sql(f"SELECT * FROM {table_name}")
            duck_db_manager.sql(f"SELECT COUNT(*) FROM {table_name}")
            duck_db_manager.sql(f"SELECT AVG(col1) FROM {table_name}")

        # Verify all connections were released back to pool
        assert (
            duck_db_manager._connection_pool.qsize() == initial_queue_size
        ), "Not all connections were released back to pool"

        # Verify we can still get all connections (no leaks)
        connections = []
        try:
            for _ in range(initial_queue_size):
                connections.append(duck_db_manager._get_connection())
            # All connections should be obtainable
            assert len(connections) == initial_queue_size
        finally:
            # Release connections back to pool
            for conn in connections:
                duck_db_manager._release_connection(conn)

    def test_multiprocess_concurrency(self, sample_df):
        """Test that multiple processes can use DuckDBConnectionManager concurrently without issues"""

        # Create a pool of processes
        num_processes = 4
        with multiprocessing.Pool(num_processes) as pool:
            # Option 1: Use starmap with tuple arguments
            results = pool.starmap(
                worker_process, [(i, sample_df) for i in range(num_processes)]
            )

        # Verify all processes completed successfully
        for result in results:
            assert result is True, f"A process failed: {result}"

        # Verify no leftover db files
        temp_dir = tempfile.gettempdir()
        leftover_files = [
            f for f in os.listdir(temp_dir) if f.startswith("pandasai_duckdb_temp_")
        ]
        assert not leftover_files, "Some temporary db files were not cleaned up"
