import java.util.Arrays;
import java.util.List;
import org.junit.Test;

public class CollectionAssertionTest {
    @Test
    public void shouldCompareArrays() {
        int[] actual = new int[] {1, 3, 5};
        assertArrayEquals(new int[] {1, 2, 5}, actual);
    }

    @Test
    public void shouldCompareCollections() {
        List<String> actual = Arrays.asList("red", "blue");
        assertIterableEquals(Arrays.asList("red", "green"), actual);
    }
}
